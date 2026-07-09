import { dashClient } from '@better-auth/infra/client';
import { createAuthClient } from 'better-auth/react';

export const authClient = createAuthClient({
  baseURL: typeof window === 'undefined' ? undefined : window.location.origin,
  plugins: [dashClient()],
});

export type Session = typeof authClient.$Infer.Session;
