import type { MiddlewareHandler } from 'hono';

import { getAuth } from '../lib/auth';

const isBetterAuthPath = (path: string): boolean =>
  path === '/api/auth' || path.startsWith('/api/auth/');

const betterAuthApi = (): MiddlewareHandler => async (c, next) => {
  if (!isBetterAuthPath(c.req.path)) {
    await next();
    return;
  }

  const auth = await getAuth();
  return await auth.handler(c.req.raw);
};

export default betterAuthApi;