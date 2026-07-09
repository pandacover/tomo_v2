import { NextResponse } from 'next/server';
import { getAuth } from '@/lib/auth';
import { dashboardEnv, requireDashboardEnv } from '@/lib/env';

export const GET = async (request: Request): Promise<Response> => {
  const auth = await getAuth();
  const session = await auth.api.getSession({ headers: request.headers });
  const url = new URL(request.url);

  if (!session?.user?.id) {
    const next = encodeURIComponent('/api/onboarding/telegram');
    return NextResponse.redirect(`${url.origin}/login?next=${next}`, 302);
  }

  const response = await fetch(`${dashboardEnv.controlApiUrl}/v1/onboarding/telegram/install-link`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-api-key': requireDashboardEnv('controlApiKey'),
    },
    body: JSON.stringify({ userId: session.user.id, email: session.user.email }),
  });

  if (!response.ok) {
    const detail = await response.text();
    return new Response(`telegram onboarding failed: ${detail}`, { status: 502 });
  }

  const payload = await response.json() as { dmUrl?: string; browserUrl?: string };
  if (!payload.dmUrl || !payload.browserUrl) {
    return new Response('telegram onboarding failed: missing telegram links', { status: 502 });
  }

  return new Response(`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>open tomo in telegram</title>
  </head>
  <body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#fff;color:#000;font-family:Arial,sans-serif;text-transform:lowercase">
    <main style="width:min(100% - 48px,480px);border:2px solid #000;padding:40px;box-shadow:12px 12px 0 #d4ff00">
      <p style="margin:0 0 12px;font-size:14px;font-weight:700;letter-spacing:.12em">tomo is ready</p>
      <h1 style="margin:0 0 16px;font-size:48px;line-height:.95">open telegram</h1>
      <p style="margin:0 0 28px;line-height:1.5">tap this to open tomo and keep your private start link attached.</p>
      <a href="${payload.dmUrl}" style="display:block;background:#000;color:#fff;padding:16px 20px;text-align:center;font-weight:700;text-decoration:none">open telegram</a>
      <a href="${payload.browserUrl}" referrerpolicy="no-referrer" style="display:block;margin-top:16px;color:#000;text-align:center">telegram not opening? use the browser link</a>
    </main>
  </body>
</html>`, {
    headers: {
      'cache-control': 'no-store',
      'content-type': 'text/html; charset=utf-8',
      'referrer-policy': 'no-referrer',
    },
  });
};
