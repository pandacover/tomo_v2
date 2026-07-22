import { afterEach, beforeEach, describe, expect, it, mock } from 'bun:test';

const getSession = mock(async () => ({ user: { id: 'session-user' } }));
const getAuth = mock(async () => ({ api: { getSession } }));
const requireDashboardEnv = mock(() => 'control-api-key');

mock.module('@/lib/auth', () => ({ getAuth }));
mock.module('@/lib/env', () => ({ dashboardEnv: { controlApiUrl: 'http://control.example' }, requireDashboardEnv }));

const { DELETE, GET, POST, PUT } = await import('../src/app/api/peers/route');
const detail = await import('../src/app/api/peers/[relationshipId]/route');
const originalFetch = globalThis.fetch;

describe('peer BFF routes', () => {
  beforeEach(() => {
    getSession.mockReset();
    getSession.mockResolvedValue({ user: { id: 'session-user' } });
    globalThis.fetch = mock(async () => new Response(JSON.stringify({ handle: 'alice', relationships: [] }), { status: 200 }));
  });

  afterEach(() => { globalThis.fetch = originalFetch; });

  it('requires a session and forwards only its user id with the control key upstream', async () => {
    const request = new Request('https://dashboard.example/api/peers');
    const response = await GET(request);
    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(globalThis.fetch).toHaveBeenCalledWith('http://control.example/v1/peers/me?userId=session-user', expect.objectContaining({ headers: { 'x-api-key': 'control-api-key' } }));
    expect(globalThis.fetch).toHaveBeenCalledWith('http://control.example/v1/peers/relationships?userId=session-user', expect.objectContaining({ headers: { 'x-api-key': 'control-api-key' } }));
    getSession.mockResolvedValue(null);
    expect((await GET(request)).status).toBe(401);
  });

  it('rejects malformed or owner-bearing requests and preserves safe upstream errors', async () => {
    expect((await POST(new Request('https://dashboard.example/api/peers', { method: 'POST', body: '{' }))).status).toBe(400);
    expect((await PUT(new Request('https://dashboard.example/api/peers', { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ handle: 'alice', userId: 'attacker' }) }))).status).toBe(400);
    globalThis.fetch = mock(async () => new Response('secret upstream detail', { status: 500 }));
    const response = await POST(new Request('https://dashboard.example/api/peers', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ peerHandle: 'bob' }) }));
    expect(response.status).toBe(502);
    expect(await response.text()).not.toContain('secret');
  });

  it('maps grant revision conflicts and only accepts exact actions', async () => {
    globalThis.fetch = mock(async () => new Response('conflict', { status: 409 }));
    const conflict = await detail.POST(new Request('https://dashboard.example/api/peers/relationship-1', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ action: 'grant', communicate: true, autoReply: false, shareAvailability: false, expectedRevision: 1, expiresAt: null }) }), { params: Promise.resolve({ relationshipId: 'relationship-1' }) });
    expect(conflict.status).toBe(409);
    expect((await detail.POST(new Request('https://dashboard.example/api/peers/relationship-1', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ action: 'accept', extra: true }) }), { params: Promise.resolve({ relationshipId: 'relationship-1' }) })).status).toBe(400);
  });

  it('purges only the authenticated relationship history and rejects every other body', async () => {
    const response = await DELETE(new Request('https://dashboard.example/api/peers', {
      method: 'DELETE',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ relationshipId: 'relationship-1', confirm: true }),
    }));

    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(globalThis.fetch).toHaveBeenCalledWith(
      'http://control.example/v1/peers/relationships/relationship-1/purge-history?userId=session-user',
      expect.objectContaining({
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-api-key': 'control-api-key' },
        body: JSON.stringify({ confirm: true, userId: 'session-user' }),
      }),
    );

    for (const body of ['{}', '{"relationshipId":"relationship-1","confirm":false}', '{"relationshipId":"relationship-1","confirm":true,"userId":"attacker"}', 'null', '{']) {
      expect((await DELETE(new Request('https://dashboard.example/api/peers', {
        method: 'DELETE',
        headers: { 'content-type': 'application/json' },
        body,
      }))).status).toBe(400);
    }
  });

  it('does not disclose purge failures or permit unauthenticated purges', async () => {
    globalThis.fetch = mock(async () => new Response('secret upstream detail', { status: 409 }));
    const conflict = await DELETE(new Request('https://dashboard.example/api/peers', {
      method: 'DELETE',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ relationshipId: 'relationship-1', confirm: true }),
    }));
    expect(conflict.status).toBe(409);
    expect(await conflict.text()).not.toContain('secret');

    globalThis.fetch = mock(async () => new Response('secret upstream detail', { status: 500 }));
    const failure = await DELETE(new Request('https://dashboard.example/api/peers', {
      method: 'DELETE',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ relationshipId: 'relationship-1', confirm: true }),
    }));
    expect(failure.status).toBe(502);
    expect(await failure.text()).not.toContain('secret');

    getSession.mockResolvedValue(null);
    expect((await DELETE(new Request('https://dashboard.example/api/peers', { method: 'DELETE', body: '{"relationshipId":"relationship-1","confirm":true}' }))).status).toBe(401);
  });
});
