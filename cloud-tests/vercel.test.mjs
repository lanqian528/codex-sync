import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import endpoint from '../api/config.mjs';
globalThis.crypto ??= webcrypto;

test('Vercel adapter: authentication, no caching, read only, updates', async () => {
  const original = Object.fromEntries(['READ_USERNAME', 'READ_PASSWORD', 'CLOUD_CONFIG'].map(k => [k, process.env[k]]));
  Object.assign(process.env, { READ_USERNAME: 'reader', READ_PASSWORD: 'fake-password', CLOUD_CONFIG: JSON.stringify({ mode: 'api', base_url: 'https://api.example.com/v1', api_key: 'fake-key' }) });
  try {
    const request = (path, method = 'GET', auth = true) => new Request('https://example.vercel.app' + path, { method, headers: auth ? { Authorization: 'Basic ' + btoa('reader:fake-password') } : {} });
    for (const path of ['/config.json', '/api/config']) {
      const res = await endpoint.fetch(request(path));
      assert.equal(res.status, 200);
      assert.match(res.headers.get('Cache-Control'), /no-store/);
      assert.match(res.headers.get('Vercel-CDN-Cache-Control'), /no-store/);
      assert.equal((await res.json()).api_key, 'fake-key');
      assert.equal((await endpoint.fetch(request(path, 'GET', false))).status, 401);
      assert.equal((await endpoint.fetch(request(path, 'POST'))).status, 405);
    }
    process.env.CLOUD_CONFIG = JSON.stringify({ mode: 'pro', base_url: 'https://new.example.com/v1', api_key: 'updated-fake-key' });
    assert.equal((await (await endpoint.fetch(request('/config.json'))).json()).mode, 'pro');
    assert.equal((await endpoint.fetch(request('/config.json?password=no'))).status, 404);
    process.env.CLOUD_CONFIG = '{}';
    assert.equal((await endpoint.fetch(request('/config.json'))).status, 503);
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});
