import { describe, expect, it, mock } from 'bun:test';

const requireDashboardEnv = mock(() => 'control-api-key');
mock.module('@/lib/env', () => ({ dashboardEnv: { controlApiUrl: 'http://control.example' }, requireDashboardEnv }));
const { getPeerDashboard } = await import('./peer-client');

describe('peer client', () => {
  it('combines the authenticated owner handle and relationships without exposing its key', async () => {
    const fetcher = mock(async (url: string) => new Response(JSON.stringify(url.endsWith('/me?userId=user-1') ? { handle: 'alice' } : { relationships: [] }), { status: 200 }));
    await expect(getPeerDashboard('user-1', fetcher)).resolves.toEqual({ handle: 'alice', relationships: [] });
    expect(fetcher).toHaveBeenCalledWith('http://control.example/v1/peers/me?userId=user-1', expect.objectContaining({ headers: { 'x-api-key': 'control-api-key' } }));
  });
});
