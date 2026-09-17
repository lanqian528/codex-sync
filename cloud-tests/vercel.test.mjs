import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import endpoint from '../api/config.mjs';
import admin from '../api/admin.mjs';
import session from '../api/session.mjs';
import { store } from '../cloud/vercel.mjs';
globalThis.crypto ??= webcrypto;
test('Vercel routes forward bodies, cookies, auth and conditional writes', async()=>{
  const keys=['ACCESS_KEY','CLOUD_CONFIG','BLOB_STORE_ID','BLOB_READ_WRITE_TOKEN'];
  const previous=Object.fromEntries(keys.map(k=>[k,process.env[k]]));
  const oldRead=store.read,oldWrite=store.write;
  const key='fake-access-key-for-tests-only-123456789';
  process.env.ACCESS_KEY=key; process.env.BLOB_STORE_ID='fake'; delete process.env.CLOUD_CONFIG;
  let saved=null;
  store.read=async()=>saved;
  store.write=async config=>{saved={config,etag:'"one"'};return saved.etag};
  const req=(path,method='GET',body,headers={})=>new Request('https://example.vercel.app'+path,{method,headers:{Authorization:'Bearer '+key,'Content-Type':'application/json',...headers},...(body?{body:JSON.stringify(body)}:{})});
  try{
    const login=await session.fetch(req('/api/session','POST',{access_key:key},{Origin:'https://example.vercel.app'}));
    assert.equal(login.status,200);assert.match(login.headers.get('set-cookie'),/HttpOnly/);
    const res=await admin.fetch(req('/api/admin','PUT',{mode:'api',base_url:'https://api.example.com/v1',api_key:'fake-key'},{'If-Match':'initial'}));
    assert.equal(res.status,200);
    const read=await endpoint.fetch(req('/config.json'));
    assert.equal((await read.json()).api_key,'fake-key');assert.match(read.headers.get('Vercel-CDN-Cache-Control'),/no-store/);
    assert.equal((await endpoint.fetch(req('/config.json','GET',null,{Authorization:''}))).status,401);
  }finally{
    store.read=oldRead;store.write=oldWrite;
    for(const[k,v]of Object.entries(previous)){if(v===undefined)delete process.env[k];else process.env[k]=v}
  }
});
