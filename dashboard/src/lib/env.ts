import path from 'node:path';

const rootDataDir = process.env.TOMO_DASHBOARD_DATA_DIR ?? process.env.TOMO_DATA_DIR ?? '.tomo_dashboard';
const fallbackAuthSecret = 'tomo-dashboard-local-build-secret-change-me';
const railwayPublicUrl = process.env.RAILWAY_PUBLIC_DOMAIN
  ? `https://${process.env.RAILWAY_PUBLIC_DOMAIN}`
  : undefined;

export const dashboardEnv = {
  betterAuthSecret: process.env.BETTER_AUTH_SECRET ?? process.env.TOMO_DASHBOARD_AUTH_SECRET ?? fallbackAuthSecret,
  betterAuthApiKey: process.env.BETTER_AUTH_API_KEY,
  betterAuthUrl: process.env.BETTER_AUTH_URL ?? railwayPublicUrl ?? 'http://localhost:3000',
  dataDir: rootDataDir,
  authDbPath: process.env.TOMO_DASHBOARD_AUTH_DB ?? path.join(rootDataDir, 'auth.sqlite'),
  controlApiUrl: (process.env.TOMO_CONTROL_API_URL ?? 'http://127.0.0.1:8787').replace(/\/$/, ''),
  controlApiKey: process.env['TOMO_' + 'CONTROL_API_KEY'],
};

export function requireDashboardEnv(name: keyof typeof dashboardEnv): string {
  const value = dashboardEnv[name];
  if (!value) {
    throw new Error(`missing dashboard env ${name}`);
  }
  return value;
}
