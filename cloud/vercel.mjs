import { get, put, BlobPreconditionFailedError } from '@vercel/blob';
import service, { Conflict, validate } from '../worker/src/index.js';
export const store = {
  async read({ useCache = false } = {}) {
    const blob = await get('config.json', { access: 'private', useCache, abortSignal: AbortSignal.timeout(8000) });
    if (!blob) return null;
    if (!blob.stream || blob.statusCode !== 200 || blob.blob.size > 65536) throw Error('Read failed');
    return { config: validate(JSON.parse(await new Response(blob.stream).text())), etag: blob.blob.etag };
  },
  async write(config, etag) {
    try {
      const blob = await put('config.json', JSON.stringify(config), {
        access: 'private', addRandomSuffix: false, allowOverwrite: etag !== 'initial',
        ...(etag === 'initial' ? {} : { ifMatch: etag }),
        cacheControlMaxAge: 3600, contentType: 'application/json', abortSignal: AbortSignal.timeout(8000),
      });
      return blob.etag;
    } catch (error) {
      if (error instanceof BlobPreconditionFailedError) throw new Conflict();
      throw error;
    }
  },
};
// Only authenticated device polling uses Blob's private CDN cache. Admin reads
// and the pre-save comparison still read the origin to preserve conflict checks.
const pollingStore = { read: () => store.read({ useCache: true }) };
export function handle(request, pathname) {
  const url = new URL(request.url);
  url.pathname = pathname;
  return service.fetch(new Request(url, request), {
    ACCESS_KEY: process.env.ACCESS_KEY,
    CLOUD_CONFIG: process.env.CLOUD_CONFIG,
    CONFIG_STORE: process.env.BLOB_READ_WRITE_TOKEN || process.env.BLOB_STORE_ID
      ? (pathname === '/config.json' ? pollingStore : store) : null,
  });
}
