import { get, put, BlobPreconditionFailedError } from '@vercel/blob';
import service, { Conflict, validate } from '../worker/src/index.js';
export const store = {
  async read() {
    const blob = await get('config.json', { access: 'private', useCache: false, abortSignal: AbortSignal.timeout(8000) });
    if (!blob) return null;
    if (!blob.stream || blob.statusCode !== 200 || blob.blob.size > 65536) throw Error('Read failed');
    return { config: validate(JSON.parse(await new Response(blob.stream).text())), etag: blob.blob.etag };
  },
  async write(config, etag) {
    try {
      const blob = await put('config.json', JSON.stringify(config), {
        access: 'private', addRandomSuffix: false, allowOverwrite: etag !== 'initial',
        ...(etag === 'initial' ? {} : { ifMatch: etag }),
        cacheControlMaxAge: 60, contentType: 'application/json', abortSignal: AbortSignal.timeout(8000),
      });
      return blob.etag;
    } catch (error) {
      if (error instanceof BlobPreconditionFailedError) throw new Conflict();
      throw error;
    }
  },
};
export function handle(request, pathname) {
  const url = new URL(request.url);
  url.pathname = pathname;
  return service.fetch(new Request(url, request), {
    ACCESS_KEY: process.env.ACCESS_KEY,
    CLOUD_CONFIG: process.env.CLOUD_CONFIG,
    CONFIG_STORE: process.env.BLOB_READ_WRITE_TOKEN || process.env.BLOB_STORE_ID ? store : null,
  });
}
