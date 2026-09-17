import { handle } from '../cloud/vercel.mjs';
export default { fetch: request => handle(request, '/api/session') };
