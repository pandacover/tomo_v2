'use client';

import { useState } from 'react';
import { authClient } from '@/lib/auth-client';

type LoginFormProps = { next?: string };

export function LoginForm({ next = '/api/onboarding/telegram' }: LoginFormProps) {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [forgot, setForgot] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const callbackURL = next.startsWith('/') && !next.startsWith('//')
    ? next
    : '/api/onboarding/telegram';

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null); setStatus(null); setBusy(true);
    try {
      if (forgot) {
        const result = await authClient.requestPasswordReset({
          email,
          redirectTo: new URL('/reset-password', window.location.origin).toString(),
        });
        if (result.error) setError('we could not send that request. try again.');
        else setStatus('if an account uses this email, a reset link is on its way.');
        return;
      }
      const result = mode === 'signup'
        ? await authClient.signUp.email({ email, password, name: name || email, callbackURL })
        : await authClient.signIn.email({ email, password, callbackURL });
      if (result.error) setError(result.error.message ?? 'could not continue. try again.');
      else window.location.assign(callbackURL);
    } catch {
      setError('we could not complete that request. try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="login-form">
      <div className="login-form-heading">
        <p className="login-form-mark">tomo</p>
        <h1>{forgot ? 'reset your password.' : mode === 'signup' ? 'meet your tomo.' : 'welcome back.'}</h1>
        <p>{forgot ? 'enter your email to receive a reset link.' : mode === 'signup' ? 'create an account to begin.' : 'pick up where you left off.'}</p>
      </div>
      {!forgot && mode === 'signup' ? (
        <label className="login-field">
          name
          <input
            className="form-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="name"
            required
          />
        </label>
      ) : null}
      <label className="login-field">
        email
        <input
          className="form-input"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          type="email"
          required
        />
      </label>
      {!forgot && <label className="login-field">
        password
        <input
          className="form-input"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
          type="password"
          required
          minLength={8}
        />
      </label>}
      {error ? <p className="login-error" role="alert">{error}</p> : null}
      {status ? <p role="status">{status}</p> : null}
      <button className="submit-button" type="submit" disabled={busy}>{busy ? (forgot ? 'sending...' : 'continuing...') : forgot ? 'send reset link' : mode === 'signup' ? 'create my tomo' : 'continue'}</button>
      {forgot ? <button className="login-mode-toggle" type="button" onClick={() => { setForgot(false); setError(null); setStatus(null); }}>back</button> : <><button className="login-mode-toggle" type="button" onClick={() => { setMode(mode === 'signin' ? 'signup' : 'signin'); setError(null); setStatus(null); }}>{mode === 'signin' ? 'new here? make an account' : 'already have an account? continue'}</button>{mode === 'signin' && <button className="login-mode-toggle" type="button" onClick={() => { setForgot(true); setError(null); setStatus(null); }}>forgot password?</button>}</>}
    </form>
  );
}
