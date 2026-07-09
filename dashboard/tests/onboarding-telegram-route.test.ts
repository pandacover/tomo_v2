import { afterEach, beforeEach, describe, expect, it, mock } from 'bun:test';

const getSession = mock(async () => ({ user: { id: 'user-1', email: 'user@example.com' } }));
const getAuth = mock(async () => ({ api: { getSession } }));
const requireDashboardEnv = mock(() => 'control-api-key');

mock.module('@/lib/auth', () => ({ getAuth }));
mock.module('@/lib/env', () => ({
  dashboardEnv: { controlApiUrl: 'http://control.example' },
  requireDashboardEnv,
}));

const { GET } = await import('../src/app/api/onboarding/telegram/route');
const originalFetch = globalThis.fetch;

describe('GET /api/onboarding/telegram', () => {
  beforeEach(() => {
    getSession.mockClear();
    requireDashboardEnv.mockClear();
    globalThis.fetch = mock(async () => new Response(JSON.stringify({
      dmUrl: 'tg://resolve?domain=tomo_bot&start=single-use-token',
      browserUrl: 'https://t.me/tomo_bot?start=single-use-token',
    }), { status: 200 }));
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('redirects to the https t.me link so browsers preserve the start token', async () => {
    const response = await GET(new Request('https://dashboard.example/api/onboarding/telegram'));

    expect(response.status).toBe(302);
    expect(response.headers.get('location')).toBe('https://t.me/tomo_bot?start=single-use-token');
  });
});
