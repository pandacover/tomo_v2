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
  if (!payload.browserUrl) {
    return new Response('telegram onboarding failed: missing telegram browser link', { status: 502 });
  }

  return NextResponse.redirect(payload.browserUrl, 302);
};
