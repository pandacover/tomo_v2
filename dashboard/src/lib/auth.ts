import fs from 'node:fs';
import path from 'node:path';
import { dash } from '@better-auth/infra';
import { betterAuth } from 'better-auth';
import { dashboardEnv, requireDashboardEnv } from './env';

type Auth = ReturnType<typeof betterAuth>;

let authPromise: Promise<Auth> | undefined;

export function getAuth(): Promise<Auth> {
  authPromise ??= createAuth();
  return authPromise;
}

async function createAuth(): Promise<Auth> {
  fs.mkdirSync(path.dirname(dashboardEnv.authDbPath), { recursive: true });
  const { default: Database } = await import('better-sqlite3');

  return betterAuth({
    secret: requireDashboardEnv('betterAuthSecret'),
    baseURL: dashboardEnv.betterAuthUrl,
    database: new Database(dashboardEnv.authDbPath),
    emailAndPassword: {
      enabled: true,
      autoSignIn: true,
    },
    trustedOrigins: [dashboardEnv.betterAuthUrl],
    plugins: dashboardEnv.betterAuthApiKey ? [dash({ apiKey: dashboardEnv.betterAuthApiKey })] : [],
  });
}

export type AuthSession = Awaited<ReturnType<Awaited<ReturnType<typeof getAuth>>['api']['getSession']>>;
