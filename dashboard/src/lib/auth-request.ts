import type { Context } from 'hono';

import { dashboardEnv } from './env';

const buildPublicAuthUrl = (incoming: URL): URL => {
  const publicBase = new URL(dashboardEnv.betterAuthUrl);
  return new URL(incoming.pathname + incoming.search, publicBase);
};

const withForwardedHostHeaders = (headers: Headers, publicUrl: URL): Headers => {
  const next = new Headers(headers);
  next.set('host', publicUrl.host);
  next.set('x-forwarded-host', publicUrl.host);
  next.set('x-forwarded-proto', publicUrl.protocol.replace(':', ''));
  return next;
};

export const toBetterAuthRequest = (c: Context): Request => {
  const incoming = new URL(c.req.url);
  const publicUrl = buildPublicAuthUrl(incoming);
  const headers = withForwardedHostHeaders(c.req.raw.headers, publicUrl);

  const init: RequestInit = {
    method: c.req.method,
    headers,
  };

  if (c.req.method !== 'GET' && c.req.method !== 'HEAD') {
    init.body = c.req.raw.body;
    // @ts-expect-error duplex required for streaming bodies in Node fetch
    init.duplex = 'half';
  }

  return new Request(publicUrl, init);
};

export const toBetterAuthRequestFromRaw = (request: Request): Request => {
  const incoming = new URL(request.url);
  const publicUrl = buildPublicAuthUrl(incoming);
  const headers = withForwardedHostHeaders(request.headers, publicUrl);
  return new Request(publicUrl, {
    method: request.method,
    headers,
    body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
    // @ts-expect-error duplex required for streaming bodies in Node fetch
    duplex: request.method === 'GET' || request.method === 'HEAD' ? undefined : 'half',
  });
};