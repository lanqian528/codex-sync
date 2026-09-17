// Shared authenticated API for Vercel and Cloudflare. No secrets are logged.
export const headers = {
  'Cache-Control': 'no-store, max-age=0', 'CDN-Cache-Control': 'no-store',
  'Vercel-CDN-Cache-Control': 'no-store', 'Pragma': 'no-cache',
  'Content-Type': 'application/json; charset=utf-8',
  'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
};
const COOKIE = '__Host-codex_sync';
const SESSION_SECONDS = 3600;
const encoder = new TextEncoder();
export class Conflict extends Error {}
class Invalid extends Error {}
function reply(status, data, extra = {}) {
  return new Response(JSON.stringify(data), { status, headers: { ...headers, ...extra } });
}
function fail(status, message) { return reply(status, { error: message }); }
async function hexHash(value) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value))), x => x.toString(16).padStart(2, '0')).join('');
}
async function equal(a, b) {
  const [x, y] = await Promise.all([hexHash(a), hexHash(b)]);
  let mismatch = 0;
  for (let i = 0; i < x.length; i++) mismatch |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return mismatch === 0;
}
async function sign(value, secret) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return Array.from(new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(value))), x => x.toString(16).padStart(2, '0')).join('');
}
function cookie(token, age = SESSION_SECONDS) {
  return `${COOKIE}=${token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${age}`;
}
async function session(request, secret) {
  const value = (request.headers.get('cookie') || '').split(';').map(x => x.trim()).find(x => x.startsWith(COOKIE + '='))?.slice(COOKIE.length + 1) || '';
  const match = /^(\d{10})\.([a-f0-9]{32})\.([a-f0-9]{64})$/.exec(value);
  if (!match) return null;
  const now = Math.floor(Date.now() / 1000);
  if (Number(match[1]) <= now || Number(match[1]) > now + SESSION_SECONDS) return null;
  if (!await equal(match[3], await sign(`${match[1]}.${match[2]}`, secret))) return null;
  return { csrf: await sign('csrf:' + value, secret) };
}
async function authentication(request, secret) {
  const auth = request.headers.get('authorization') || '';
  if (auth) {
    // Basic sync:<ACCESS_KEY> permits deliberate migration from older clients.
    const basic = 'Basic ' + btoa('sync:' + secret);
    return await equal(auth, 'Bearer ' + secret) || await equal(auth, basic) ? { bearer: true } : null;
  }
  return session(request, secret);
}
async function body(request) {
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json')) throw new Invalid();
  if (Number(request.headers.get('content-length')) > 65536) throw new Invalid();
  const reader = request.body?.getReader();
  if (!reader) throw new Invalid();
  let size = 0;
  const chunks = [];
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 65536) { await reader.cancel(); throw new Invalid(); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    const data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Invalid();
    return data;
  } catch { throw new Invalid(); }
}
export function validate(data) {
  if (!data || Object.keys(data).sort().join(',') !== 'api_key,base_url,mode' || !['pro', 'api'].includes(data.mode)) throw new Invalid();
  if (typeof data.base_url !== 'string' || /\s|[\x00-\x1f]/.test(data.base_url)) throw new Invalid();
  let url;
  try { url = new URL(data.base_url); } catch { throw new Invalid(); }
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) throw new Invalid();
  if (typeof data.api_key !== 'string' || /[^\x21-\x7e]/.test(data.api_key) || data.api_key.length > 8192) throw new Invalid();
  if (data.mode === 'api' && (!data.api_key || data.api_key.startsWith('REPLACE_'))) throw new Invalid();
  return { mode: data.mode, base_url: data.base_url, api_key: data.api_key };
}
function r2Store(bucket) {
  return {
    async read() {
      const object = await bucket.get('config.json');
      if (!object) return null;
      return { config: validate(JSON.parse(await object.text())), etag: object.httpEtag };
    },
    async write(config, etag) {
      const result = await bucket.put('config.json', JSON.stringify(config), {
        httpMetadata: { contentType: 'application/json', cacheControl: 'no-store' },
        onlyIf: etag === 'initial' ? { etagDoesNotMatch: '*' } : { etagMatches: etag.replaceAll('"', '') },
      });
      if (!result) throw new Conflict();
      return result.httpEtag;
    },
  };
}
async function readConfig(env, store) {
  const saved = store ? await store.read() : null;
  if (saved) return { config: validate(saved.config), etag: saved.etag };
  const initial = env.CLOUD_CONFIG ? JSON.parse(env.CLOUD_CONFIG) : { mode: 'pro', base_url: 'https://api.example.com/v1', api_key: '' };
  return { config: validate(initial), etag: 'initial' };
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.protocol !== 'https:') return fail(400, 'HTTPS required');
    const paths = ['/config.json', '/api/config', '/api/session', '/api/admin'];
    if (!paths.includes(url.pathname)) {
      if (env.ASSETS && ['GET', 'HEAD'].includes(request.method) && ['/', '/index.html', '/app.js', '/style.css'].includes(url.pathname)) return env.ASSETS.fetch(request);
      return fail(404, 'Not found');
    }
    if (url.search) return fail(400, 'Query parameters are not allowed');
    const secret = env.ACCESS_KEY;
    if (typeof secret !== 'string' || !/^[\x21-\x7e]{32,256}$/.test(secret)) return fail(503, 'Set ACCESS_KEY to a random key of 32–256 characters');
    try {
      if (url.pathname === '/api/session' && request.method === 'POST') {
        if (request.headers.get('origin') !== url.origin) return fail(403, 'Same-origin request required');
        const input = await body(request);
        if (Object.keys(input).join(',') !== 'access_key' || typeof input.access_key !== 'string' || !await equal(input.access_key, secret)) return fail(401, 'Invalid access key');
        const expiry = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
        const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');
        const unsigned = `${expiry}.${nonce}`;
        const token = unsigned + '.' + await sign(unsigned, secret);
        return reply(200, { ok: true, csrf: await sign('csrf:' + token, secret) }, { 'Set-Cookie': cookie(token) });
      }
      const auth = await authentication(request, secret);
      if (!auth) return reply(401, { error: 'Authentication required' }, { 'WWW-Authenticate': 'Bearer realm="codex-sync"' });
      if (!['GET', 'HEAD'].includes(request.method) && !auth.bearer) {
        if (request.headers.get('origin') !== url.origin || !await equal(request.headers.get('x-csrf-token') || '', auth.csrf)) return fail(403, 'CSRF check failed');
      }
      if (url.pathname === '/api/session') {
        if (request.method === 'GET') return reply(200, { ok: true, csrf: auth.csrf || null });
        if (request.method === 'DELETE') return reply(200, { ok: true }, { 'Set-Cookie': cookie('', 0) });
        return fail(405, 'Method not allowed');
      }
      const store = env.CONFIG_STORE || (env.CONFIG_BUCKET ? r2Store(env.CONFIG_BUCKET) : null);
      if (url.pathname === '/config.json' || url.pathname === '/api/config') {
        if (request.method !== 'GET') return fail(405, 'Read only');
        return reply(200, (await readConfig(env, store)).config);
      }
      if (request.method === 'GET') {
        const { config, etag } = await readConfig(env, store);
        return reply(200, { mode: config.mode, base_url: config.base_url, has_api_key: Boolean(config.api_key), storage_ready: Boolean(store) }, { ETag: etag });
      }
      if (request.method === 'PUT') {
        if (!store) return fail(503, 'Connect private storage before saving');
        const expected = request.headers.get('if-match');
        if (!expected) return fail(428, 'Reload configuration before saving');
        const input = await body(request);
        if (!Object.hasOwn(input, 'mode') || !Object.hasOwn(input, 'base_url') || Object.keys(input).some(k => !['mode', 'base_url', 'api_key'].includes(k))) throw new Invalid();
        const previous = await readConfig(env, store);
        if (expected !== previous.etag) throw new Conflict();
        const config = validate({ ...previous.config, ...input });
        const etag = await store.write(config, expected);
        return reply(200, { mode: config.mode, base_url: config.base_url, has_api_key: Boolean(config.api_key), storage_ready: true }, { ETag: etag });
      }
      return fail(405, 'Method not allowed');
    } catch (error) {
      if (error instanceof Invalid) return fail(400, 'Invalid input: check mode, HTTPS URL and API key');
      if (error instanceof Conflict) return fail(409, 'Configuration changed elsewhere; reload before saving');
      return fail(503, 'Configuration storage unavailable; no successful save confirmed');
    }
  },
};
