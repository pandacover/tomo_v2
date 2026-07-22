import fs from 'node:fs';
import path from 'node:path';
import { createEmailSender, dash } from '@better-auth/infra';
import { betterAuth } from 'better-auth';
import { getMigrations } from 'better-auth/db/migration';
import { dashboardEnv, requireDashboardEnv } from './env';
import { deliverResetPasswordEmail } from './reset-password';

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

  const auth = betterAuth({
    secret: requireDashboardEnv('betterAuthSecret'),
    baseURL: dashboardEnv.betterAuthUrl,
    database: await openAuthDatabase(dashboardEnv.authDbPath),
    emailAndPassword: {
      enabled: true,
      autoSignIn: true,
      revokeSessionsOnPasswordReset: true,
      sendResetPassword: async ({ user, url }) => {
        await deliverResetPasswordEmail(
          createEmailSender({ apiKey: requireDashboardEnv('betterAuthApiKey') }),
          { user, url },
        );
      },
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

  await ensureAuthSchema(auth.options);
  return auth;
}

async function ensureAuthSchema(options: Parameters<typeof getMigrations>[0]): Promise<void> {
  const { toBeCreated, toBeAdded, runMigrations } = await getMigrations(options);
  if (toBeCreated.length > 0 || toBeAdded.length > 0) {
    await runMigrations();
  }
}

export type AuthSession = Awaited<ReturnType<Awaited<ReturnType<typeof getAuth>>['api']['getSession']>>;
