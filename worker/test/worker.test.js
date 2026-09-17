import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import worker, { Conflict } from '../src/index.js';
globalThis.crypto ??= webcrypto;
const KEY = 'fake-access-key-for-tests-only-123456789';
const config = { mode: 'api', base_url: 'https://api.example.com/v1', api_key: 'fake-api-key' };
function fixture() {
  let value = null, counter = 0;
  const env = { ACCESS_KEY: KEY, CONFIG_STORE: {
    async read() { return value ? structuredClone(value) : null; },
    async write(config, etag) {
      if ((value?.etag || 'initial') !== etag) throw new Conflict();
      value = { config, etag: `"etag-${++counter}"` };
      return value.etag;
    },
  } };
  return { env, saved: () => value };
}
function req(path = '/config.json', method = 'GET', data, extra = {}) {
  return new Request('https://example.com' + path, { method, headers: { Authorization: 'Bearer ' + KEY, ...(data ? { 'Content-Type': 'application/json' } : {}), ...extra }, ...(data ? { body: JSON.stringify(data) } : {}) });
}
test('unauthenticated, wrong and absent keys never read or write storage', async () => {
  const { env, saved } = fixture();
  for (const path of ['/config.json', '/api/admin', '/api/session']) {
    const res = await worker.fetch(req(path, 'GET', null, { Authorization: '' }), env);
    assert.equal(res.status, 401); assert.match(res.headers.get('Cache-Control'), /no-store/);
  }
  assert.equal((await worker.fetch(req('/api/admin', 'PUT', config, { Authorization: 'Bearer wrong', 'X-Config-Revision': 'initial' }), env)).status, 401);
  assert.equal((await worker.fetch(req(), { ...env, ACCESS_KEY: '' })).status, 503);
  assert.equal(saved(), null);
});
test('same access key reads, edits and survives a new handler invocation', async () => {
  const { env } = fixture();
  const saved = await worker.fetch(req('/api/admin', 'PUT', config, { 'X-Config-Revision': 'initial' }), env);
  assert.equal(saved.status, 200);
  assert.equal((await saved.json()).has_api_key, true);
  const read = await worker.fetch(req(), { ...env });
  assert.deepEqual(await read.json(), config);
  const admin = await worker.fetch(req('/api/admin'), env);
  const redacted = await admin.json();
  assert.ok(!('api_key' in redacted));
  assert.equal((await worker.fetch(req('/config.json', 'GET', null, { Authorization: 'Basic ' + btoa('sync:' + KEY) }), env)).status, 200);
});
test('Pro preserves key, replacement updates it, stale writes fail', async () => {
  const { env, saved } = fixture();
  let res = await worker.fetch(req('/api/admin', 'PUT', config, { 'X-Config-Revision': 'initial' }), env);
  const etag = (await res.clone().json()).revision;
  res = await worker.fetch(req('/api/admin', 'PUT', { mode: 'pro', base_url: 'https://new.example/v1' }, { 'X-Config-Revision': etag }), env);
  assert.equal(res.status, 200); assert.equal(saved().config.api_key, config.api_key);
  assert.equal((await worker.fetch(req('/api/admin', 'PUT', config, { 'X-Config-Revision': etag }), env)).status, 409);
  res = await worker.fetch(req('/api/admin', 'PUT', { ...config, api_key: 'new-fake-key' }, { 'X-Config-Revision': (await res.clone().json()).revision }), env);
  assert.equal(res.status, 200); assert.equal(saved().config.api_key, 'new-fake-key');
});
test('invalid input, unsupported method and missing precondition do not save', async () => {
  const { env, saved } = fixture();
  for (const data of [{...config, mode:'bad'}, {...config, base_url:'http://bad.test'}, {...config, api_key:''}, {...config, extra:1}, {...config, api_key:'secret\nheader'}]) {
    assert.equal((await worker.fetch(req('/api/admin','PUT',data,{'X-Config-Revision':'initial'}),env)).status,400);
  }
  assert.equal((await worker.fetch(req('/api/admin','PUT',config),env)).status,428);
  assert.equal((await worker.fetch(req('/config.json','POST',config),env)).status,405);
  assert.equal((await worker.fetch(req('/config.json?key=secret'),env)).status,400);
  assert.equal(saved(),null);
});
test('login issues secure cookie; CSRF, logout and key rotation', async () => {
  const { env } = fixture();
  const login = await worker.fetch(req('/api/session','POST',{access_key:KEY},{Origin:'https://example.com',Authorization:''}),env);
  assert.equal(login.status,200);
  const setCookie=login.headers.get('set-cookie');
  for (const flag of ['HttpOnly','Secure','SameSite=Strict','Path=/']) assert.ok(setCookie.includes(flag));
  const cookie=setCookie.split(';')[0], {csrf}=await login.json();
  const auth={Authorization:'',Cookie:cookie,Origin:'https://example.com','X-CSRF-Token':csrf,'X-Config-Revision':'initial'};
  assert.equal((await worker.fetch(req('/api/admin','GET',null,auth),env)).status,200);
  assert.equal((await worker.fetch(req('/api/admin','PUT',config,{...auth,'X-CSRF-Token':''}),env)).status,403);
  assert.equal((await worker.fetch(req('/api/admin','PUT',config,{...auth,Origin:'https://evil.test'}),env)).status,403);
  assert.equal((await worker.fetch(req('/api/admin','PUT',config,auth),env)).status,200);
  const logout=await worker.fetch(req('/api/session','DELETE',null,auth),env);
  assert.match(logout.headers.get('set-cookie'),/Max-Age=0/);
  assert.equal((await worker.fetch(req('/api/admin','GET',null,auth),{...env,ACCESS_KEY:KEY+'rotated'})).status,401);
  assert.equal((await worker.fetch(req('/api/session','POST',{access_key:KEY},{Origin:'https://evil.test'}),env)).status,403);
  assert.equal((await worker.fetch(req('/api/session','POST',{access_key:'wrong'},{Origin:'https://example.com'}),env)).status,401);
});
test('storage failure is redacted and never becomes a successful save', async () => {
  const { env }=fixture();
  env.CONFIG_STORE.read=async()=>{throw Error('PRIVATE-SECRET')};
  const res=await worker.fetch(req(),env);
  assert.equal(res.status,503); assert.ok(!(await res.text()).includes('PRIVATE-SECRET'));
});
test('unconfigured storage is read-only, API key stays out of admin responses',async()=>{
  const env={ACCESS_KEY:KEY,CLOUD_CONFIG:JSON.stringify(config)};
  const res=await worker.fetch(req('/api/admin'),env);
  assert.equal((await res.json()).storage_ready,false);
  assert.equal((await worker.fetch(req('/api/admin','PUT',config,{'X-Config-Revision':'initial'}),env)).status,503);
});
test('saves work without HTTP ETag, including initial and consecutive saves', async () => {
  const { env, saved } = fixture();
  const firstRead = await worker.fetch(req('/api/admin'), env);
  assert.equal(firstRead.headers.get('etag'), null);
  const initial = await firstRead.json();
  assert.equal(initial.revision, 'initial');
  const firstSave = await worker.fetch(req('/api/admin', 'PUT', config, { 'X-Config-Revision': initial.revision }), env);
  assert.equal(firstSave.status, 200);
  assert.equal(firstSave.headers.get('etag'), null);
  const updated = await firstSave.json();
  assert.ok(updated.revision && updated.revision !== initial.revision);
  const secondSave = await worker.fetch(req('/api/admin', 'PUT', { mode: 'pro', base_url: config.base_url }, { 'X-Config-Revision': updated.revision }), env);
  assert.equal(secondSave.status, 200);
  assert.equal(saved().config.api_key, config.api_key);
  // A different page still holding an older token must never overwrite it.
  assert.equal((await worker.fetch(req('/api/admin', 'PUT', config, { 'X-Config-Revision': updated.revision }), env)).status, 409);
});
test('old pages sending missing ETag as literal null get refresh guidance', async () => {
  const { env, saved } = fixture();
  const response = await worker.fetch(req('/api/admin', 'PUT', config, { 'If-Match': 'null' }), env);
  assert.equal(response.status, 428);
  assert.equal(saved(), null);
});
