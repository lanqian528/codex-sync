const headers = {
  'Cache-Control': 'no-store, max-age=0',
  'CDN-Cache-Control': 'no-store',
  'Vercel-CDN-Cache-Control': 'no-store',
  'Pragma': 'no-cache',
  'Content-Type': 'application/json; charset=utf-8',
  'X-Content-Type-Options': 'nosniff',
};

async function equal(a, b) {
  const hash = async s => new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)));
  const [x, y] = await Promise.all([hash(a), hash(b)]);
  let mismatch = 0;
  for (let i = 0; i < x.length; i++) mismatch |= x[i] ^ y[i];
  return mismatch === 0;
}

export default {
  async fetch(request, env) {
    const reply = (status, message, extra = {}) => new Response(JSON.stringify({ error: message }), { status, headers: { ...headers, ...extra } });
    const url = new URL(request.url);
    if (url.protocol !== 'https:') return reply(400, 'HTTPS required');
    if (url.pathname !== '/config.json' || url.search) return reply(404, 'Not found');
    if (request.method !== 'GET') return reply(405, 'Read only', { Allow: 'GET' });
    if (!env.READ_USERNAME || !env.READ_PASSWORD || !env.CLOUD_CONFIG) return reply(503, 'Not configured');
    const expected = 'Basic ' + btoa(unescape(encodeURIComponent(env.READ_USERNAME + ':' + env.READ_PASSWORD)));
    if (!await equal(request.headers.get('Authorization') || '', expected)) {
      return reply(401, 'Unauthorized', { 'WWW-Authenticate': 'Basic realm="codex-sync", charset="UTF-8"' });
    }
    try {
      const data = JSON.parse(env.CLOUD_CONFIG);
      if (Object.keys(data).sort().join(',') !== 'api_key,base_url,mode' || !['pro', 'api'].includes(data.mode)) throw Error();
      const base = new URL(data.base_url);
      if (base.protocol !== 'https:' || base.username || base.password || base.search || base.hash) throw Error();
      if (typeof data.api_key !== 'string' || (data.mode === 'api' && !data.api_key) || /[^\x21-\x7e]/.test(data.api_key)) throw Error();
      return new Response(JSON.stringify(data), { headers });
    } catch {
      return reply(503, 'Invalid configuration');
    }
  },
};
