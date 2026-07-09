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
    <form onSubmit={submit} className="mx-auto flex max-w-md flex-col gap-4 rounded-[2rem] border border-tomo-ink/10 bg-white/70 p-8 shadow-xl backdrop-blur">
      <h1 className="font-heading text-4xl font-black text-tomo-ink">text tomo</h1>
      <p className="text-sm text-tomo-ink/70">sign in first, then we&apos;ll open telegram and bind your chat.</p>
      {mode === 'signup' ? (
        <input className="rounded-full border px-4 py-3" value={name} onChange={(e) => setName(e.target.value)} placeholder="name" />
      ) : null}
      <input className="rounded-full border px-4 py-3" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="email" type="email" required />
      <input className="rounded-full border px-4 py-3" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password" type="password" required minLength={8} />
      {error ? <p className="text-sm text-red-600">{error}</p> : null}
      <button className="rounded-full bg-tomo-ink px-6 py-3 font-heading font-black uppercase tracking-widest text-white" type="submit">
        continue
      </button>
      <button className="text-sm underline" type="button" onClick={() => setMode(mode === 'signin' ? 'signup' : 'signin')}>
        {mode === 'signin' ? 'new here? make an account' : 'already have an account? sign in'}
      </button>
    </form>
  );
}
