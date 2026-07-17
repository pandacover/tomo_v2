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
  const callbackURL = next || '/api/onboarding/telegram';

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    const result = mode === 'signup'
      ? await authClient.signUp.email({ email, password, name: name || email, callbackURL })
      : await authClient.signIn.email({ email, password, callbackURL });
    if (result.error) setError(result.error.message ?? 'auth failed');
  }

  return (
    <form onSubmit={submit} className="login-form">
      <div className="login-form-heading">
        <p className="login-form-mark">tomo</p>
        <h1>continue the conversation.</h1>
        <p>Sign in to connect this conversation with Telegram.</p>
      </div>
      {mode === 'signup' ? (
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
      <label className="login-field">
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
      </label>
      {error ? <p className="login-error" role="alert">{error}</p> : null}
      <button className="submit-button" type="submit">continue to telegram</button>
      <button className="login-mode-toggle" type="button" onClick={() => setMode(mode === 'signin' ? 'signup' : 'signin')}>
        {mode === 'signin' ? 'new here? make an account' : 'already have an account? sign in'}
      </button>
    </form>
  );
}
