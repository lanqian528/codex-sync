import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import worker from '../src/index.js';
globalThis.crypto ??= webcrypto;
const env = { READ_USERNAME: 'reader', READ_PASSWORD: 'fake-password', CLOUD_CONFIG: JSON.stringify({ mode: 'api', base_url: 'https://example.com/v1', api_key: 'fake-api-key' }) };
const request = (auth = true, method = 'GET') => new Request('https://example.workers.dev/config.json', { method, headers: auth ? { Authorization: 'Basic ' + btoa('reader:fake-password') } : {} });
test('authenticated exact JSON, no cache', async () => {
  const res = await worker.fetch(request(), env);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('Cache-Control'), /no-store/);
  assert.deepEqual(await res.json(), JSON.parse(env.CLOUD_CONFIG));
});
test('missing/wrong authentication does not disclose key', async () => {
  for (const r of [request(false), new Request('https://example.workers.dev/config.json', { headers: { Authorization: 'Basic invalid' } })]) {
    const res = await worker.fetch(r, env);
    assert.equal(res.status, 401);
    assert.ok(!(await res.text()).includes('fake-api-key'));
  }
});
test('no public write endpoint', async () => assert.equal((await worker.fetch(request(true, 'POST'), env)).status, 405));
test('bad secret fails closed', async () => assert.equal((await worker.fetch(request(), { ...env, CLOUD_CONFIG: '{}' })).status, 503));
