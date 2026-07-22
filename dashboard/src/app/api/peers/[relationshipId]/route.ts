import { getAuth } from '@/lib/auth';
import { getPeerHistory } from '@/lib/peer-client';
import { upstream } from '../route';

const noStore = { 'cache-control': 'no-store', 'content-type': 'application/json; charset=utf-8' };
const response = (body: object, status = 200) => new Response(JSON.stringify(body), { status, headers: noStore });
type Context = { params: Promise<{ relationshipId: string }> };

async function userId(request: Request): Promise<string | null> { const auth = await getAuth(); const session = await auth.api.getSession({ headers: request.headers }); return session?.user?.id ?? null; }
async function body(request: Request): Promise<unknown> { try { return await request.json(); } catch { return null; } }
const exact = (value: unknown, keys: string[]) => typeof value === 'object' && value !== null && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every((key) => key in value);
const validExpiry = (value: unknown) => {
  if (value === null) return true;
  if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return false;
  const parsed = new Date(value);
  return !Number.isNaN(parsed.valueOf()) && parsed.valueOf() > Date.now();
};

export async function GET(request: Request, context: Context): Promise<Response> {
  const id = await userId(request); if (!id) return response({ error: 'unauthorized' }, 401);
  try { return response({ history: await getPeerHistory(id, (await context.params).relationshipId) }); } catch (error) { return response({ error: error instanceof Error && error.message === 'control_404' ? 'not found' : 'peer service unavailable' }, error instanceof Error && error.message === 'control_404' ? 404 : 502); }
}

export async function POST(request: Request, context: Context): Promise<Response> {
  const id = await userId(request); if (!id) return response({ error: 'unauthorized' }, 401);
  const value = await body(request); const relationshipId = (await context.params).relationshipId;
  if (exact(value, ['action']) && (value as { action: unknown }).action === 'accept') return upstream(id, `/v1/peers/relationships/${encodeURIComponent(relationshipId)}/accept`, 'POST', {});
  if (exact(value, ['action']) && ((value as { action: unknown }).action === 'revoke' || (value as { action: unknown }).action === 'block')) {
    const action = (value as { action: 'revoke' | 'block' }).action;
    return upstream(id, `/v1/peers/relationships/${encodeURIComponent(relationshipId)}/${action}`, 'POST', {});
  }
  if (exact(value, ['action', 'communicate', 'autoReply', 'shareAvailability', 'expectedRevision', 'expiresAt']) && (value as { action: unknown }).action === 'grant') {
    const grant = value as { communicate: unknown; autoReply: unknown; shareAvailability: unknown; expectedRevision: unknown; expiresAt: unknown };
    if (typeof grant.communicate === 'boolean' && typeof grant.autoReply === 'boolean' && typeof grant.shareAvailability === 'boolean' && typeof grant.expectedRevision === 'number' && Number.isInteger(grant.expectedRevision) && grant.expectedRevision >= 0 && validExpiry(grant.expiresAt)) return upstream(id, `/v1/peers/relationships/${encodeURIComponent(relationshipId)}/grant`, 'PATCH', { communicate: grant.communicate, autoReply: grant.autoReply, shareAvailability: grant.shareAvailability, expectedRevision: grant.expectedRevision, expiresAt: grant.expiresAt as string | null });
  }
  return response({ error: 'invalid request' }, 400);
}
