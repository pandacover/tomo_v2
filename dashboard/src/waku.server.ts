import type { Context, MiddlewareHandler } from 'hono';
import { fsRouter } from 'waku';
import adapter from 'waku/adapters/node';

import { getAuth } from './lib/auth';

const betterAuthRouteMethods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'] as const;

const forwardBetterAuth = async (c: Context) => {
  const auth = await getAuth();
  return await auth.handler(c.req.raw);
};

function registerBetterAuthRoutes({ app }: { app: import('hono').Hono }): MiddlewareHandler {
  for (const method of betterAuthRouteMethods) {
    app.on(method, '/api/auth', forwardBetterAuth);
    app.on(method, '/api/auth/*', forwardBetterAuth);
  }

  return async (c, next) => next();
}

export default adapter(fsRouter(import.meta.glob('./pages/**/*.{tsx,ts}')), {
  middlewareFns: [registerBetterAuthRoutes],
});