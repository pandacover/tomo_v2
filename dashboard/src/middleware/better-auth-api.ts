import type { MiddlewareHandler } from 'hono';

import { getAuth } from '../lib/auth';

const isBetterAuthPath = (pathname: string): boolean =>
  pathname === '/api/auth' || pathname.startsWith('/api/auth/');

const betterAuthApi = (): MiddlewareHandler => async (c, next) => {
  const pathname = new URL(c.req.url).pathname;
  if (!isBetterAuthPath(pathname)) {
    await next();
    return;
  }

  const auth = await getAuth();
  const response = await auth.handler(c.req.raw);
  c.res = response;
  return response;
};

export default betterAuthApi;