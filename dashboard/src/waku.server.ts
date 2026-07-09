import type { MiddlewareHandler } from 'hono';
import { fsRouter } from 'waku';
import adapter from 'waku/adapters/node';

import { getAuth } from './lib/auth';

const isBetterAuthPath = (pathname: string): boolean =>
  pathname === '/api/auth' || pathname.startsWith('/api/auth/');

const withMiddlewareMarker = (response: Response): Response => {
  const headers = new Headers(response.headers);
  headers.set('x-tomo-auth-middleware', '1');
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
};

function registerBetterAuthRoutes(_opts: { app: import('hono').Hono }): MiddlewareHandler {
  return async (c, next) => {
    const pathname = new URL(c.req.url).pathname;
    if (!isBetterAuthPath(pathname)) {
      await next();
      return;
    }

    const auth = await getAuth();
    const response = withMiddlewareMarker(await auth.handler(c.req.raw));
    c.res = response;
    return response;
  };
}

export default adapter(fsRouter(import.meta.glob('./pages/**/*.{tsx,ts}')), {
  middlewareFns: [registerBetterAuthRoutes],
});