import { getAuth } from '@/lib/auth';
import { getPeerDashboard, mutatePeer, purgePeerHistory } from '@/lib/peer-client';

const noStore = { 'cache-control': 'no-store', 'content-type': 'application/json; charset=utf-8' };
const response = (body: object, status = 200) => new Response(JSON.stringify(body), { status, headers: noStore });
const handlePattern = /^[a-z0-9_]{3,32}$/;

async function userId(request: Request): Promise<string | null> {
  const auth = await getAuth();
  const session = await auth.api.getSession({ headers: request.headers });
  return session?.user?.id ?? null;
}

async function body(request: Request): Promise<unknown> { try { return await request.json(); } catch { return null; } }
const exact = (value: unknown, keys: string[]) => typeof value === 'object' && value !== null && !Array.isArray(value) && Object.keys(value).every((key) => keys.includes(key)) && keys.every((key) => key in value);

export async function GET(request: Request): Promise<Response> {
  const id = await userId(request);
  if (!id) return response({ error: 'unauthorized' }, 401);
  try { return response(await getPeerDashboard(id)); } catch { return response({ error: 'peer service unavailable' }, 502); }
}

export async function PUT(request: Request): Promise<Response> {
  const id = await userId(request); if (!id) return response({ error: 'unauthorized' }, 401);
  const value = await body(request);
  if (!exact(value, ['handle']) || typeof (value as { handle: unknown }).handle !== 'string' || !handlePattern.test((value as { handle: string }).handle)) return response({ error: 'invalid request' }, 400);
  return upstream(id, '/v1/peers/me/handle', 'PUT', value as object);
}

export async function POST(request: Request): Promise<Response> {
  const id = await userId(request); if (!id) return response({ error: 'unauthorized' }, 401);
  const value = await body(request);
  if (!exact(value, ['peerHandle']) || typeof (value as { peerHandle: unknown }).peerHandle !== 'string' || !handlePattern.test((value as { peerHandle: string }).peerHandle)) return response({ error: 'invalid request' }, 400);
  return upstream(id, '/v1/peers/invitations', 'POST', value as object);
}

export async function DELETE(request: Request): Promise<Response> {
  const id = await userId(request);
  if (!id) return response({ error: 'unauthorized' }, 401);
  const value = await body(request);
  if (!exact(value, ['relationshipId', 'confirm']) || typeof (value as { relationshipId: unknown }).relationshipId !== 'string' || !(value as { relationshipId: string }).relationshipId.trim() || (value as { confirm: unknown }).confirm !== true) return response({ error: 'invalid request' }, 400);

  try {
    const result = await purgePeerHistory(id, (value as { relationshipId: string }).relationshipId);
    if (result.status === 409) return response({ error: 'purge conflict' }, 409);
    if (!result.ok) return response({ error: 'peer service unavailable' }, 502);
    return new Response(await result.text(), { status: result.status, headers: noStore });
  } catch {
    return response({ error: 'peer service unavailable' }, 502);
  }
}

export async function upstream(id: string, path: string, method: 'POST' | 'PUT' | 'PATCH', value: object): Promise<Response> {
  try {
    const result = await mutatePeer(id, path, method, value);
    if (result.status === 409) return response({ error: 'stale revision' }, 409);
    if (!result.ok) return response({ error: 'peer service unavailable' }, 502);
    return new Response(await result.text(), { status: result.status, headers: noStore });
  } catch { return response({ error: 'peer service unavailable' }, 502); }
}
