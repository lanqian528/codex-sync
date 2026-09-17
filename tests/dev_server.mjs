// Isolated UI test fixture. Fake key and in-memory data only; never deploy this file.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import service, { Conflict } from '../worker/src/index.js';
let saved = null, sequence = 0;
const env = { ACCESS_KEY: 'fake-access-key-for-tests-only-123456789', CONFIG_STORE: {
  async read() { return saved; },
  async write(config, etag) {
    if ((saved?.etag || 'initial') !== etag) throw new Conflict();
    saved = { config, etag: `"test-${++sequence}"` };
    return saved.etag;
  },
} };
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname;
  if (['/', '/index.html', '/app.js', '/style.css'].includes(path)) {
    const file = path === '/' ? 'index.html' : path.slice(1);
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(await readFile(new URL('../public/' + file, import.meta.url))); return;
  }
  const chunks=[];
  for await(const chunk of req)chunks.push(chunk);
  const headers = new Headers(req.headers);
  // Only this loopback fixture maps HTTP localhost to the production HTTPS contract.
  if (headers.get('origin') === 'http://' + req.headers.host) headers.set('origin', 'https://' + req.headers.host);
  const request = new Request('https://' + req.headers.host + req.url, { method:req.method, headers, ...(!['GET','HEAD'].includes(req.method)?{body:Buffer.concat(chunks)}:{}) });
  const response=await service.fetch(request,env);
  res.writeHead(response.status,Object.fromEntries(response.headers));
  res.end(Buffer.from(await response.arrayBuffer()));
});
server.listen(0,'127.0.0.1',()=>console.log('UI fixture: http://localhost:'+server.address().port));
