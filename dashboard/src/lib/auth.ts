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

type SqliteDatabase = unknown;

async function openAuthDatabase(dbPath: string): Promise<SqliteDatabase> {
  if (typeof Bun !== 'undefined') {
    const { Database } = await import('bun:sqlite');
    return new Database(dbPath, { create: true });
  }

  const [majorRaw, minorRaw] = process.versions.node.split('.');
  const major = Number(majorRaw);
  const minor = Number(minorRaw ?? 0);
  if (major > 22 || (major === 22 && minor >= 5)) {
    const { DatabaseSync } = await import('node:sqlite');
    return new DatabaseSync(dbPath);
  }

  const { default: Database } = await import('better-sqlite3');
  return new Database(dbPath);
}

async function createAuth(): Promise<Auth> {
  fs.mkdirSync(path.dirname(dashboardEnv.authDbPath), { recursive: true });
  const database = await openAuthDatabase(dashboardEnv.authDbPath);
  const hostedAuth = !/localhost|127\.0\.0\.1/i.test(dashboardEnv.betterAuthUrl);

  return betterAuth({
    secret: requireDashboardEnv('betterAuthSecret'),
    baseURL: dashboardEnv.betterAuthUrl,
    database,
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
    plugins: dashboardEnv.betterAuthApiKey ? [dash({ apiKey: dashboardEnv.betterAuthApiKey })] : [],
  });
}

export type AuthSession = Awaited<ReturnType<Awaited<ReturnType<typeof getAuth>>['api']['getSession']>>;