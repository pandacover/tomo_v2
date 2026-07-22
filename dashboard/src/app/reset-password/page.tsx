'use client';

import Link from 'next/link';
import Image from 'next/image';
import { useSearchParams } from 'next/navigation';
import { Suspense, useState, type ReactNode } from 'react';
import { authClient } from '@/lib/auth-client';
import { hero } from '@/lib/landing-content';

export default function ResetPasswordPage() {
  return <ResetShell><Suspense fallback={<main className="reset-page"><p role="status">loading reset form...</p></main>}><ResetPasswordForm /></Suspense></ResetShell>;
}

function ResetShell({ children }: { children: ReactNode }) {
  return <section className="login-page"><div className="login-frame"><Image className="login-image" src={hero.image} alt="" fill preload sizes="100vw" /><div className="login-scrim" /><div className="login-form-layer">{children}</div></div></section>;
}

function ResetPasswordForm() {
  const params = useSearchParams();
  const token = params.get('token');
  const queryError = params.get('error');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState(queryError ? 'this reset link is no longer valid.' : '');
  const [success, setSuccess] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError('');
    if (!token) return setError('this reset link is missing its token.');
    if (password.length < 8) return setError('use at least 8 characters.');
    if (password !== confirmation) return setError('passwords do not match.');
    setBusy(true);
    try {
      const result = await authClient.resetPassword({ newPassword: password, token });
      if (result.error) setError('this reset link is invalid or expired. request a new one.');
      else setSuccess(true);
    } catch {
      setError('we could not update your password. try again.');
    } finally {
      setBusy(false);
    }
  }

  return <main className="reset-page"><div className="reset-card"><p className="login-form-mark">tomo</p><h1>choose a new password.</h1>{success ? <><p role="status">your password has been updated.</p><Link className="action-link" href="/login">continue</Link></> : <form className="reset-form" onSubmit={submit} aria-busy={busy}><label className="login-field">new password<input className="form-input" type="password" minLength={8} required value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" /></label><label className="login-field">confirm password<input className="form-input" type="password" minLength={8} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="new-password" /></label>{error && <p className="login-error" role="alert">{error}</p>}<button className="submit-button" disabled={busy || !token}>{busy ? 'updating...' : 'update password'}</button></form>}</div></main>;
}
