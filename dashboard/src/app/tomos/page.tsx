import type { Metadata } from 'next';
import { redirect } from 'next/navigation';
import { TomoRelationships } from '@/components/tomo-relationships';
import { getAuth } from '@/lib/auth';
import { getPeerDashboard } from '@/lib/peer-client';

export const metadata: Metadata = { title: 'tomo connections' };

export default async function TomosPage() {
  const auth = await getAuth();
  const session = await auth.api.getSession({ headers: await import('next/headers').then(({ headers }) => headers()) });
  if (!session?.user?.id) redirect('/login?next=/tomos');
  let initial = null;
  try { initial = await getPeerDashboard(session.user.id); } catch {}
  return <TomoRelationships initial={initial} />;
}
