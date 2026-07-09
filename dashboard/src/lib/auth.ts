import fs from 'node:fs';
import path from 'node:path';
import { dash } from '@better-auth/infra';
import { betterAuth } from 'better-auth';
import { dashboardEnv, requireDashboardEnv } from './env';

type SqliteDatabase = unknown;

let authPromise: ReturnType<typeof createAuth> | undefined;

export function getAuth() {
  authPromise ??= createAuth();
  return authPromise;
}

async function openAuthDatabase(dbPath: string): Promise<SqliteDatabase> {
  if (typeof Bun !== 'undefined') {
    const { Database } = await import('bun:sqlite');
    return new Database(dbPath, { create: true });
  }

  const { DatabaseSync } = await import('node:sqlite');
  return new DatabaseSync(dbPath);
}

async function createAuth() {
  fs.mkdirSync(path.dirname(dashboardEnv.authDbPath), { recursive: true });

  const hostedAuth = !/localhost|127\.0\.0\.1/i.test(dashboardEnv.betterAuthUrl);
  const dashPlugin = hostedAuth
    ? dash({ apiKey: requireDashboardEnv('betterAuthApiKey') })
    : dashboardEnv.betterAuthApiKey
      ? dash({ apiKey: dashboardEnv.betterAuthApiKey })
      : undefined;

  return betterAuth({
    secret: requireDashboardEnv('betterAuthSecret'),
    baseURL: dashboardEnv.betterAuthUrl,
    database: await openAuthDatabase(dashboardEnv.authDbPath),
    emailAndPassword: {
      enabled: true,
      autoSignIn: true,
    },
    trustedOrigins: [dashboardEnv.betterAuthUrl],
    ...(hostedAuth
      ? {
          advanced: {
            trustedProxyHeaders: true,
            ipAddress: {
              ipAddressHeaders: ['x-forwarded-for', 'x-real-ip', 'cf-connecting-ip'],
            },
          },
        }
      : {}),
    plugins: dashPlugin ? [dashPlugin] : [],
  });
}

export type AuthSession = Awaited<ReturnType<Awaited<ReturnType<typeof getAuth>>['api']['getSession']>>;
