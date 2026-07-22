import { dashboardEnv, requireDashboardEnv } from '@/lib/env';
import type { PeerDashboard, PeerHistoryEntry, PeerRelationship } from './peer-types';

type Fetcher = typeof fetch;

if (typeof window !== 'undefined') {
  throw new Error('peer control client is server-only');
}

async function control(path: string, userId: string, init: RequestInit = {}, fetcher: Fetcher = fetch): Promise<Response> {
  return fetcher(`${dashboardEnv.controlApiUrl}${path}${path.includes('?') ? '&' : '?'}userId=${encodeURIComponent(userId)}`, {
    ...init,
    headers: { ...init.headers, 'x-api-key': requireDashboardEnv('controlApiKey') },
    cache: 'no-store',
  });
}

async function payload<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`control_${response.status}`);
  return response.json() as Promise<T>;
}

export async function getPeerDashboard(userId: string, fetcher?: Fetcher): Promise<PeerDashboard> {
  const [me, relationships] = await Promise.all([
    payload<{ handle: string | null }>(await control('/v1/peers/me', userId, {}, fetcher)),
    payload<{ relationships: PeerRelationship[] }>(await control('/v1/peers/relationships', userId, {}, fetcher)),
  ]);
  return { handle: me.handle, relationships: relationships.relationships };
}

export async function getPeerHistory(userId: string, relationshipId: string, fetcher?: Fetcher): Promise<PeerHistoryEntry[]> {
  return (await payload<{ history: PeerHistoryEntry[] }>(await control(`/v1/peers/relationships/${encodeURIComponent(relationshipId)}/history`, userId, {}, fetcher))).history;
}

export async function mutatePeer(userId: string, path: string, method: 'POST' | 'PUT' | 'PATCH', body: object, fetcher?: Fetcher): Promise<Response> {
  return control(path, userId, { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ...body, userId }) }, fetcher);
}

export async function purgePeerHistory(userId: string, relationshipId: string, fetcher?: Fetcher): Promise<Response> {
  return control(`/v1/peers/relationships/${encodeURIComponent(relationshipId)}/purge-history`, userId, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ confirm: true, userId }),
  }, fetcher);
}
