import service from '../worker/src/index.js';

// Vercel adapter; authentication and validation are shared with Cloudflare.
export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === '/api/config') url.pathname = '/config.json';
    return service.fetch(new Request(url, {
      method: request.method,
      headers: request.headers,
    }), process.env);
  },
};
