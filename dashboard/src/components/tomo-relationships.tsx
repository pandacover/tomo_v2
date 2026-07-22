'use client';

import { useEffect, useState, type FormEvent } from 'react';
import Link from 'next/link';
import type { PeerDashboard, PeerHistoryEntry, PeerRelationship } from '@/lib/peer-types';

const handlePattern = /^[a-z0-9_]{3,32}$/;
const trustCopy = 'another tomo can ask yours questions. it cannot use your tools, accounts, files, credentials, or authority.';
const privacyCopy = 'purging permanently removes this connection history from your views and export. shared records are deleted from storage after the other owner also purges them. it cannot retract information already disclosed.';

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(path, { ...init, headers: { 'content-type': 'application/json', ...init?.headers }, cache: 'no-store' });
  if (!response.ok) throw new Error(response.status === 409 ? 'stale' : 'request failed');
  return response.json();
}

export function TomoRelationships({ initial }: { initial: PeerDashboard | null }) {
  const [dashboard, setDashboard] = useState(initial);
  const [handle, setHandle] = useState(initial?.handle ?? '');
  const [handleStatus, setHandleStatus] = useState('');
  const [invite, setInvite] = useState('');
  const [inviteStatus, setInviteStatus] = useState('');
  const [privacyStatus, setPrivacyStatus] = useState('');
  const [purgeTarget, setPurgeTarget] = useState('');
  const [purgeConfirmation, setPurgeConfirmation] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(initial === null);
  const [busy, setBusy] = useState('');
  const [history, setHistory] = useState<Record<string, PeerHistoryEntry[]>>({});

  async function refresh() {
    const value = await request('/api/peers') as PeerDashboard;
    setDashboard(value); setHandle(value.handle ?? ''); setError(''); setLoading(false);
  }

  useEffect(() => {
    if (initial !== null) return;
    const timer = window.setTimeout(() => void refresh().catch(() => { setLoading(false); setError('connections could not be loaded. try again.'); }), 0);
    return () => window.clearTimeout(timer);
  }, [initial]);

  async function saveHandle(event: FormEvent) {
    event.preventDefault(); setHandleStatus('');
    const value = handle.trim().toLowerCase();
    if (!handlePattern.test(value)) return setHandleStatus('use 3 to 32 lowercase letters, numbers, or underscores.');
    if (busy) return;
    setBusy('handle');
    try { await request('/api/peers', { method: 'PUT', body: JSON.stringify({ handle: value }) }); await refresh(); setHandleStatus('handle saved.'); }
    catch { setHandleStatus('could not save handle.'); } finally { setBusy(''); }
  }

  async function sendInvite(event: FormEvent) {
    event.preventDefault(); setInviteStatus('');
    const value = invite.trim().toLowerCase();
    if (!handlePattern.test(value)) return setInviteStatus('enter a valid public handle.');
    if (busy) return;
    setBusy('invite');
    try { await request('/api/peers', { method: 'POST', body: JSON.stringify({ peerHandle: value }) }); await refresh(); setInvite(''); setInviteStatus('invite submitted.'); }
    catch { setInviteStatus('could not send invite.'); } finally { setBusy(''); }
  }

  async function purgeHistory(relationshipId: string) {
    if (busy || purgeConfirmation !== 'PURGE') { setPrivacyStatus('type PURGE to confirm permanent history removal.'); return; }
    setBusy('purge'); setPrivacyStatus('');
    try { await request('/api/peers', { method: 'DELETE', body: JSON.stringify({ relationshipId, confirm: true }) }); setHistory((current) => ({ ...current, [relationshipId]: [] })); setPurgeConfirmation(''); setPrivacyStatus('this connection history was purged.'); }
    catch { setPrivacyStatus('could not purge your peer history.'); } finally { setBusy(''); }
  }

  async function action(relationship: PeerRelationship, body: object) {
    if (busy) return;
    setBusy(relationship.relationshipId); setError('');
    try { await request(`/api/peers/${relationship.relationshipId}`, { method: 'POST', body: JSON.stringify(body) }); await refresh(); }
    catch (cause) {
      if (cause instanceof Error && cause.message === 'stale') { setError('this relationship changed elsewhere. the latest settings were loaded.'); try { await refresh(); } catch { /* retain conflict */ } }
      else setError('could not save this connection.');
    } finally { setBusy(''); }
  }

  async function loadHistory(relationship: PeerRelationship) {
    const id = relationship.relationshipId;
    if (history[id]) { setHistory((current) => { const next = { ...current }; delete next[id]; return next; }); return; }
    if (busy) return;
    setBusy(`history-${id}`);
    try { const value = await request(`/api/peers/${id}`) as { history: PeerHistoryEntry[] }; setHistory((current) => ({ ...current, [id]: value.history })); }
    catch { setError('could not load recent exchanges.'); } finally { setBusy(''); }
  }

  return <section className="tomos-page">
    <header className="tomos-header"><Link href="/login?next=/tomos" className="brand-mark">tomo</Link><nav aria-label="connections navigation"><a href="/api/onboarding/telegram">open telegram</a></nav></header>
    <main className="tomos-main">
      <p className="field-label">trust controls</p><h1 className="font-heading">tomo connections</h1><p className="tomos-trust">{trustCopy}</p>
      <ConnectionForms handle={handle} setHandle={setHandle} invite={invite} setInvite={setInvite} busy={busy} handleStatus={handleStatus} inviteStatus={inviteStatus} onHandleSubmit={saveHandle} onInviteSubmit={sendInvite} />
      {error && <p className="tomos-error" role="alert">{error} {!dashboard && <button onClick={() => void refresh().catch(() => setError('connections could not be loaded. try again.'))}>retry</button>}</p>}
      {loading && <p role="status">loading connections...</p>}
      {!loading && !dashboard ? null : dashboard && (dashboard.relationships.length === 0 ? <p className="tomos-empty">no tomo connections yet.</p> : <div className="tomos-list">{dashboard.relationships.map((relationship) => <RelationshipRow key={`${relationship.relationshipId}:${relationship.relationshipRevision}:${relationship.grant?.revision ?? 0}`} relationship={relationship} busy={busy} history={history[relationship.relationshipId]} onAction={action} onHistory={loadHistory} />)}</div>)}
      {dashboard && dashboard.relationships.length > 0 && <PrivacyControls relationships={dashboard.relationships} selected={purgeTarget || dashboard.relationships[0].relationshipId} onSelect={setPurgeTarget} confirmation={purgeConfirmation} onConfirmation={setPurgeConfirmation} onPurge={purgeHistory} busy={Boolean(busy)} status={privacyStatus} />}
    </main>
  </section>;
}

function ConnectionForms({ handle, setHandle, invite, setInvite, busy, handleStatus, inviteStatus, onHandleSubmit, onInviteSubmit }: { handle: string; setHandle: (value: string) => void; invite: string; setInvite: (value: string) => void; busy: string; handleStatus: string; inviteStatus: string; onHandleSubmit: (event: FormEvent) => Promise<void>; onInviteSubmit: (event: FormEvent) => Promise<void> }) {
  return <><form className="tomos-form" onSubmit={onHandleSubmit} aria-busy={Boolean(busy)}><label htmlFor="public-handle">your public handle</label><div><input id="public-handle" className="form-input" value={handle} onChange={(event) => setHandle(event.target.value)} autoComplete="off" aria-describedby="handle-status" disabled={Boolean(busy)} /><button className="submit-button" disabled={Boolean(busy)}>{busy === 'handle' ? 'saving...' : 'save handle'}</button></div><p id="handle-status" role="status">{handleStatus}</p></form><form className="tomos-form" onSubmit={onInviteSubmit} aria-busy={Boolean(busy)}><label htmlFor="peer-handle">invite by public handle</label><div><input id="peer-handle" className="form-input" value={invite} onChange={(event) => setInvite(event.target.value)} autoComplete="off" disabled={Boolean(busy)} /><button className="submit-button" disabled={Boolean(busy)}>{busy === 'invite' ? 'sending...' : 'send invite'}</button></div><p role="status">{inviteStatus}</p></form></>;
}

function RelationshipRow({ relationship, busy, history, onAction, onHistory }: { relationship: PeerRelationship; busy: string; history?: PeerHistoryEntry[]; onAction: (relationship: PeerRelationship, body: object) => Promise<void>; onHistory: (relationship: PeerRelationship) => Promise<void> }) {
  const [communicate, setCommunicate] = useState(relationship.grant?.communicate ?? false);
  const [autoReply, setAutoReply] = useState(relationship.grant?.autoReply ?? false);
  const [availability, setAvailability] = useState(relationship.grant?.shareAvailability ?? false);
  const [expiry, setExpiry] = useState(relationship.grant?.expiresAt ?? 'never');
  const submitting = Boolean(busy);
  const save = () => onAction(relationship, { action: 'grant', communicate, autoReply, shareAvailability: availability, expectedRevision: relationship.grant?.revision ?? 0, expiresAt: expiry === 'never' ? null : expiry === '7' || expiry === '30' ? new Date(Date.now() + (expiry === '7' ? 7 : 30) * 86400000).toISOString() : expiry });
  if (relationship.status === 'pending') return <article className="tomos-row"><h2>{relationship.peerHandle}</h2>{relationship.canAccept ? <button className="submit-button" disabled={submitting} onClick={() => void onAction(relationship, { action: 'accept' })}>accept</button> : <p>waiting for acceptance</p>}</article>;
  return <article className="tomos-row"><div className="tomos-row-title"><h2>{relationship.peerHandle}</h2><span>{relationship.status}</span></div>{relationship.status === 'active' && <><GrantSummary label="you to peer" grant={relationship.grant} /><GrantSummary label="peer to you" grant={relationship.peerGrant} /><label><input type="checkbox" checked={communicate} onChange={(event) => setCommunicate(event.target.checked)} disabled={submitting} /> let my tomo message this tomo</label><label><input type="checkbox" checked={autoReply} onChange={(event) => setAutoReply(event.target.checked)} disabled={submitting} /> let my tomo answer this tomo automatically</label><label><input type="checkbox" checked={availability} onChange={(event) => setAvailability(event.target.checked)} disabled={submitting} /> allow coarse availability answers</label><label>expiry<select value={expiry} onChange={(event) => setExpiry(event.target.value)} disabled={submitting}><option value="never">never</option>{!['never', '7', '30'].includes(expiry) && <option value={expiry}>{new Date(expiry).toLocaleString()}</option>}<option value="7">7 days</option><option value="30">30 days</option></select></label><button className="submit-button" disabled={submitting} onClick={() => void save()}>{busy === relationship.relationshipId ? 'saving...' : 'save grant'}</button><div className="tomos-row-actions"><button onClick={() => void onHistory(relationship)} disabled={submitting}>{history ? 'hide recent exchanges' : 'recent exchanges'}</button><button onClick={() => void onAction(relationship, { action: 'revoke' })} disabled={submitting}>revoke</button><button onClick={() => void onAction(relationship, { action: 'block' })} disabled={submitting}>block</button></div>{history && <ThreadHistory history={history} />}</>}</article>;
}

function GrantSummary({ label, grant }: { label: string; grant: PeerRelationship['grant'] }) {
  if (!grant) return <p>{label}: no grant.</p>;
  return <p>{label}: messaging {grant.communicate ? 'on' : 'off'}, automatic replies {grant.autoReply ? 'on' : 'off'}, availability {grant.shareAvailability ? 'on' : 'off'}, revision {grant.revision}, expires {grant.expiresAt ? new Date(grant.expiresAt).toLocaleString() : 'never'}.</p>;
}

function PrivacyControls({ relationships, selected, onSelect, confirmation, onConfirmation, onPurge, busy, status }: { relationships: PeerRelationship[]; selected: string; onSelect: (value: string) => void; confirmation: string; onConfirmation: (value: string) => void; onPurge: (relationshipId: string) => Promise<void>; busy: boolean; status: string }) {
  return <section className="tomos-privacy" aria-labelledby="peer-privacy-heading"><p className="field-label">privacy</p><h2 id="peer-privacy-heading">purge connection history</h2><p>{privacyCopy}</p><label htmlFor="purge-relationship">connection</label><select id="purge-relationship" value={selected} onChange={(event) => onSelect(event.target.value)} disabled={busy}>{relationships.map((relationship) => <option key={relationship.relationshipId} value={relationship.relationshipId}>{relationship.peerHandle}</option>)}</select><label htmlFor="purge-confirmation">type PURGE to confirm</label><input id="purge-confirmation" value={confirmation} onChange={(event) => onConfirmation(event.target.value)} disabled={busy} /><button className="submit-button" onClick={() => void onPurge(selected)} disabled={busy}>purge selected history</button><p role="status">{status}</p></section>;
}

function ThreadHistory({ history }: { history: PeerHistoryEntry[] }) {
  if (history.length === 0) return <p className="tomos-history">no recent exchanges.</p>;
  const threads = history.reduce<Record<string, PeerHistoryEntry[]>>((grouped, entry) => { (grouped[entry.threadId] ??= []).push(entry); return grouped; }, {});
  return <div className="tomos-history" aria-label="recent exchange audit history">{Object.entries(threads).map(([threadId, entries]) => <section key={threadId}><h3>thread {threadId.slice(0, 8)}</h3><ol>{entries.map((item) => <li key={item.requestId}>{item.direction} {item.kind.replace('_', ' ')}, {item.status}, {new Date(item.createdAt).toLocaleString()}{item.responseStatus ? `, response ${item.responseStatus}` : ''}</li>)}</ol></section>)}</div>;
}
