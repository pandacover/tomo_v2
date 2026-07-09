export const GET = async (): Promise<Response> =>
  new Response(JSON.stringify({ ok: true, service: 'dashboard-auth-route' }), {
    headers: { 'content-type': 'application/json' },
  });