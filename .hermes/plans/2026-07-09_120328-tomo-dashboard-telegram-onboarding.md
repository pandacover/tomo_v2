# tomo dashboard telegram onboarding implementation plan

> **for hermes:** use subagent-driven-development skill to implement this plan task-by-task.

**goal:** build a structured hosted onboarding flow where a dashboard visitor clicks “text tomo”, authenticates with better auth, gets redirected into telegram, presses `/start`, and then all shared-bot messages route to that user’s isolated tomo runtime instance.

**architecture:** keep exactly one shared telegram poller for the global bot token. the dashboard owns browser auth and calls a narrow python control api to mint short-lived telegram install links. the shared gateway consumes `/start <token>`, binds `chat_id -> user_id -> tomo_id`, ensures a per-user runtime instance, and routes every later telegram update to that instance.

**tech stack:** waku + react + better auth + sqlite on the dashboard; python 3.11 + fastapi/uvicorn + sqlite + existing `tomo_core` runtime on the control/shared-gateway side; telegram bot api long polling for v1.

---

## current context / assumptions

- repo root inspected: `C:\Users\luvma\OneDrive\Desktop\zero_labs\tomo_v2`.
- dirty worktree at planning time:
  - modified: `tomo_core/.tomo_core/sessions/telegram_actor_5995349219.json`
  - modified: `tomo_core/SOUL.md`
  - untracked: `dashboard/`, `ref.text`
- current dashboard is a waku app under `dashboard/`, currently static pages only.
- current `dashboard/package.json` has no auth deps yet. it uses `waku`, `hono`, react 19, tailwind 4, and `bun.lock` exists.
- current cta is `dashboard/src/components/landing-hero.tsx:31-35`, linking straight to `/api/onboarding/telegram`.
- current tomo core is a small python package under `tomo_core/` with a single-user telegram polling bot:
  - `tomo_core/src/tomo_core/telegram_bot.py`
  - `tomo_core/src/tomo_core/runtime.py`
  - `tomo_core/src/tomo_core/sessions.py`
  - `tomo_core/src/tomo_core/cli.py`
- current runtime session identity is dm-only: `InboundEnvelope.session_key == "telegram:actor:{actor_id}"` in `tomo_core/src/tomo_core/models.py:35-38`.
- better auth docs checked from `https://better-auth.com/llms.txt` via curl because the web extract tool was not configured. relevant docs:
  - waku integration: `src/auth.ts`, `src/pages/_api/api/auth/[...route].ts`, `better-auth/react` client, `auth.api.getSession({ headers })`.
  - sqlite adapter: recommended `better-sqlite3` with `database: new Database("database.sqlite")`.
  - basic usage: enable `emailAndPassword`, use client `signIn.email` / `signUp.email`.
- assumption: “auth at better-auth” means in-app auth powered by better auth, not redirecting users to `better-auth.com` as an identity provider. if the product literally wants better auth infrastructure hosted login screens, swap the dashboard login page for that provider, but keep the control-api/onboarding/gateway seams unchanged.

## locked product rules

1. do not create a personal bot token per user.
2. do not start one telegram poller per user.
3. one global bot token means one shared gateway/listener.
4. “new instance per user” means a new tomo runtime/profile/sandbox keyed by `tomo_id`, not a separate telegram inbox consumer.
5. sign-in alone does not bind telegram. the binding proof is `/start <short_lived_token>` from telegram.
6. install tokens must be short-lived and single-use; store only a hash of the token.
7. dashboard never receives or returns telegram bot tokens.
8. runtime state must be per-user under canonical data dir, e.g. `.tomo_core/instances/{tomo_id}/`.
9. keep one global soul/persona; do not fork `SOUL.md` per user.
10. preserve unrelated dirty files, especially current `SOUL.md` and session json.

## proposed flow

```text
browser
  -> landing page: click "text tomo"
  -> /login?next=/api/onboarding/telegram if no valid better-auth session
  -> better auth session cookie set
  -> /api/onboarding/telegram dashboard bff
  -> python control api POST /v1/onboarding/telegram/install-link
  -> 302 tg://resolve?domain=<global_bot_username>&start=<token>

telegram
  -> shared gateway receives /start <token>
  -> onboarding store consumes token and records chat_id
  -> shared gateway ensures runtime instance for tomo_id
  -> sends "tomo is connected. text me."
  -> later dm updates route by chat_id to that tomo_id runtime
```

---

## phase 1: dashboard better auth

### task 1: add dashboard auth dependencies

**objective:** install better auth and sqlite driver for the waku dashboard.

**files:**
- modify: `dashboard/package.json`
- modify: `dashboard/bun.lock`

**step 1: install deps**

run from `dashboard/`:

```bash
bun add better-auth better-sqlite3
```

expected: `package.json` gains `better-auth` and `better-sqlite3` dependencies.

**step 2: verify package graph**

```bash
bun run typegen
bun run build
```

expected: build may fail until auth files exist; dependency resolution should not fail.

**optional commit only if user requested commits:**

```bash
git add dashboard/package.json dashboard/bun.lock
git commit -m "feat: add dashboard auth dependencies"
```

### task 2: create dashboard env helpers

**objective:** centralize env parsing so auth, bff routes, and deployment config are not scattered.

**files:**
- create: `dashboard/src/lib/env.ts`

**step 1: write `dashboard/src/lib/env.ts`**

```ts
import path from 'node:path';

const rootDataDir = process.env.TOMO_DASHBOARD_DATA_DIR ?? process.env.TOMO_DATA_DIR ?? '.tomo_dashboard';

export const dashboardEnv = {
  betterAuthSecret: process.env.BETTER_AUTH_SECRET ?? process.env.TOMO_DASHBOARD_AUTH_SECRET,
  betterAuthUrl: process.env.BETTER_AUTH_URL ?? 'http://localhost:3000',
  dataDir: rootDataDir,
  authDbPath: process.env.TOMO_DASHBOARD_AUTH_DB ?? path.join(rootDataDir, 'auth.sqlite'),
  controlApiUrl: (process.env.TOMO_CONTROL_API_URL ?? 'http://127.0.0.1:8787').replace(/\/$/, ''),
  controlApiKey: process.env.TOMO_CONTROL_API_KEY,
};

export function requireDashboardEnv(name: keyof typeof dashboardEnv): string {
  const value = dashboardEnv[name];
  if (!value) {
    throw new Error(`missing dashboard env ${name}`);
  }
  return value;
}
```

**step 2: run typecheck/build**

```bash
cd dashboard && bun run build
```

expected: may still fail until later imports exist; this file should typecheck.

### task 3: create better auth server instance

**objective:** configure better auth with sqlite and email/password sign-in.

**files:**
- create: `dashboard/src/lib/auth.ts`

**step 1: write auth instance**

```ts
import fs from 'node:fs';
import path from 'node:path';
import { betterAuth } from 'better-auth';
import Database from 'better-sqlite3';
import { dashboardEnv, requireDashboardEnv } from './env';

fs.mkdirSync(path.dirname(dashboardEnv.authDbPath), { recursive: true });

export const auth = betterAuth({
  secret: requireDashboardEnv('betterAuthSecret'),
  baseURL: dashboardEnv.betterAuthUrl,
  database: new Database(dashboardEnv.authDbPath),
  emailAndPassword: {
    enabled: true,
    autoSignIn: true,
  },
  trustedOrigins: [dashboardEnv.betterAuthUrl],
});

export type AuthSession = Awaited<ReturnType<typeof auth.api.getSession>>;
```

**step 2: generate/migrate better auth schema**

run from `dashboard/` after auth route exists if the cli requires route discovery:

```bash
bun x auth@latest generate
bun x auth@latest migrate
```

expected: better auth creates required auth tables in `TOMO_DASHBOARD_AUTH_DB` or `.tomo_dashboard/auth.sqlite`.

### task 4: mount better auth api route in waku

**objective:** expose `/api/auth/*` so sign-in, sign-up, session, and callback endpoints work.

**files:**
- create: `dashboard/src/pages/_api/api/auth/[...route].ts`

**step 1: write route handler**

```ts
import { auth } from '../../../../lib/auth';

export const GET = async (request: Request): Promise<Response> => auth.handler(request);
export const POST = async (request: Request): Promise<Response> => auth.handler(request);
```

**step 2: verify route compiles**

```bash
cd dashboard && bun run typegen && bun run build
```

expected: no missing route/import errors.

### task 5: create better auth client

**objective:** allow client components to sign in/sign up and inspect session.

**files:**
- create: `dashboard/src/lib/auth-client.ts`

**step 1: write client**

```ts
import { createAuthClient } from 'better-auth/react';

export const authClient = createAuthClient({
  baseURL: typeof window === 'undefined' ? undefined : window.location.origin,
});

export type Session = typeof authClient.$Infer.Session;
```

**step 2: verify**

```bash
cd dashboard && bun run build
```

expected: `better-auth/react` resolves.

### task 6: add login page with next redirect

**objective:** provide a simple auth page that signs users in/up and then continues to the telegram onboarding bff route.

**files:**
- create: `dashboard/src/components/login-form.tsx`
- create: `dashboard/src/pages/login.tsx`
- modify: `dashboard/src/pages.gen.ts` only via `bun run typegen`, not manual editing unless waku requires it.

**step 1: write client login form**

```tsx
'use client';

import { useMemo, useState } from 'react';
import { authClient } from '../lib/auth-client';

type LoginFormProps = { next?: string };

export function LoginForm({ next = '/api/onboarding/telegram' }: LoginFormProps) {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const callbackURL = useMemo(() => next || '/api/onboarding/telegram', [next]);

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
      <p className="text-sm text-tomo-ink/70">sign in first, then we’ll open telegram and bind your chat.</p>
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
```

**step 2: write page**

```tsx
import { LoginForm } from '../components/login-form';

export default async function LoginPage({ searchParams }: { searchParams?: { next?: string } }) {
  return (
    <section className="mx-auto max-w-tomo-container px-tomo-gutter py-24">
      <LoginForm next={searchParams?.next ?? '/api/onboarding/telegram'} />
    </section>
  );
}

export const getConfig = async () => ({ render: 'dynamic' }) as const;
```

**step 3: verify**

```bash
cd dashboard && bun run typegen && bun run build
```

expected: login route is generated and build passes.

---

## phase 2: python onboarding control api

### task 7: add backend http dependencies

**objective:** add a minimal python http control plane for dashboard bff calls.

**files:**
- modify: `tomo_core/pyproject.toml`

**step 1: update dependencies**

add:

```toml
  "fastapi>=0.115",
  "uvicorn>=0.30",
```

**step 2: sync and verify imports**

```bash
cd tomo_core && uv sync && uv run python -c "import fastapi, uvicorn; print('ok')"
```

expected: prints `ok`.

### task 8: create hashed install-token store

**objective:** persist short-lived install links and chat bindings without storing raw tokens.

**files:**
- create: `tomo_core/src/tomo_core/onboarding_store.py`
- test: `tomo_core/tests/test_onboarding_store.py`

**step 1: write failing tests**

```python
import tempfile
import unittest

from tomo_core.onboarding_store import TelegramOnboardingStore


class TelegramOnboardingStoreTests(unittest.TestCase):
    def test_create_and_consume_single_use_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link(user_id="user-1", bot_username="tmnvm_bot")

            self.assertTrue(link.dm_url.startswith("tg://resolve?domain=tmnvm_bot&start="))
            installation = store.consume_start_token(link.token, chat_id="123", actor_id="123")

            self.assertEqual(installation.user_id, "user-1")
            self.assertEqual(installation.chat_id, "123")
            self.assertTrue(installation.tomo_id.startswith("tomo-user-1-"))
            self.assertIsNone(store.consume_start_token(link.token, chat_id="123", actor_id="123"))

    def test_lookup_installation_by_chat_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link(user_id="user-2", bot_username="tmnvm_bot")
            installation = store.consume_start_token(link.token, chat_id="999", actor_id="888")

            self.assertEqual(store.installation_for_chat("999"), installation)
```

**step 2: run test and verify failure**

```bash
cd tomo_core && uv run python -m unittest tests.test_onboarding_store -v
```

expected: fail because module does not exist.

**step 3: implement store**

```python
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallLink:
    token: str
    dm_url: str
    browser_url: str
    expires_at: int


@dataclass(frozen=True)
class TelegramInstallation:
    user_id: str
    tomo_id: str
    chat_id: str
    actor_id: str
    installed_at: int


class TelegramOnboardingStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "onboarding.sqlite"
        self._init_db()

    def create_install_link(self, user_id: str, bot_username: str, ttl_seconds: int = 600) -> InstallLink:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + ttl_seconds
        tomo_id = self._tomo_id_for_user(user_id)
        with self._connect() as db:
            db.execute(
                """
                insert into telegram_install_tokens(token_hash, user_id, tomo_id, expires_at, consumed_at, created_at)
                values (?, ?, ?, ?, null, ?)
                """,
                (self._hash(token), user_id, tomo_id, expires_at, now),
            )
        return InstallLink(
            token=token,
            dm_url=f"tg://resolve?domain={bot_username}&start={token}",
            browser_url=f"https://t.me/{bot_username}?start={token}",
            expires_at=expires_at,
        )

    def consume_start_token(self, token: str, chat_id: str, actor_id: str) -> TelegramInstallation | None:
        now = int(time.time())
        token_hash = self._hash(token)
        with self._connect() as db:
            row = db.execute(
                """
                select user_id, tomo_id, expires_at, consumed_at from telegram_install_tokens
                where token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None or row["consumed_at"] is not None or int(row["expires_at"]) < now:
                return None
            db.execute("update telegram_install_tokens set consumed_at = ? where token_hash = ?", (now, token_hash))
            db.execute(
                """
                insert into telegram_installations(user_id, tomo_id, chat_id, actor_id, installed_at)
                values (?, ?, ?, ?, ?)
                on conflict(chat_id) do update set
                  user_id = excluded.user_id,
                  tomo_id = excluded.tomo_id,
                  actor_id = excluded.actor_id,
                  installed_at = excluded.installed_at
                """,
                (row["user_id"], row["tomo_id"], chat_id, actor_id, now),
            )
        return TelegramInstallation(row["user_id"], row["tomo_id"], chat_id, actor_id, now)

    def installation_for_chat(self, chat_id: str) -> TelegramInstallation | None:
        with self._connect() as db:
            row = db.execute(
                "select user_id, tomo_id, chat_id, actor_id, installed_at from telegram_installations where chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return TelegramInstallation(row["user_id"], row["tomo_id"], row["chat_id"], row["actor_id"], int(row["installed_at"]))

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute("""
                create table if not exists telegram_install_tokens(
                  token_hash text primary key,
                  user_id text not null,
                  tomo_id text not null,
                  expires_at integer not null,
                  consumed_at integer,
                  created_at integer not null
                )
            """)
            db.execute("""
                create table if not exists telegram_installations(
                  chat_id text primary key,
                  user_id text not null,
                  tomo_id text not null,
                  actor_id text not null,
                  installed_at integer not null
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _tomo_id_for_user(user_id: str) -> str:
        suffix = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:10]
        safe_user = "".join(ch if ch.isalnum() else "-" for ch in user_id.lower()).strip("-")[:32] or "user"
        return f"tomo-{safe_user}-{suffix}"
```

**step 4: verify**

```bash
cd tomo_core && uv run python -m unittest tests.test_onboarding_store -v
```

expected: 2 passed.

### task 9: add control api endpoint for install links

**objective:** let the dashboard bff mint install links with an authenticated dashboard user id.

**files:**
- create: `tomo_core/src/tomo_core/control_api.py`
- test: `tomo_core/tests/test_control_api.py`

**step 1: write failing tests**

```python
import tempfile
import unittest

import httpx

from tomo_core.control_api import create_app


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_link_requires_api_key_and_returns_no_raw_secret_fields_besides_deeplink_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp, api_key="secret", bot_username="tmnvm_bot")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                denied = await client.post("/v1/onboarding/telegram/install-link", json={"userId": "u1"})
                self.assertEqual(denied.status_code, 401)

                ok = await client.post(
                    "/v1/onboarding/telegram/install-link",
                    headers={"x-api-key": "secret"},
                    json={"userId": "u1"},
                )

            self.assertEqual(ok.status_code, 200)
            payload = ok.json()
            self.assertTrue(payload["dmUrl"].startswith("tg://resolve?domain=tmnvm_bot&start="))
            self.assertTrue(payload["browserUrl"].startswith("https://t.me/tmnvm_bot?start="))
            self.assertIn("expiresAt", payload)
            self.assertNotIn("botToken", payload)
```

**step 2: run test to verify failure**

```bash
cd tomo_core && uv run python -m unittest tests.test_control_api -v
```

expected: fail because `control_api` does not exist.

**step 3: implement app**

```python
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .onboarding_store import TelegramOnboardingStore


class InstallLinkRequest(BaseModel):
    user_id: str = Field(alias="userId", min_length=1)
    email: str | None = None


class InstallLinkResponse(BaseModel):
    dm_url: str = Field(alias="dmUrl")
    browser_url: str = Field(alias="browserUrl")
    expires_at: int = Field(alias="expiresAt")


def create_app(data_dir: str | Path | None = None, api_key: str | None = None, bot_username: str | None = None) -> FastAPI:
    resolved_data_dir = Path(data_dir or os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    resolved_api_key = api_key if api_key is not None else os.getenv("TOMO_CONTROL_API_KEY")
    resolved_bot_username = bot_username or os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME")
    store = TelegramOnboardingStore(resolved_data_dir)
    app = FastAPI(title="tomo core control api")

    @app.get("/v1/health")
    def health() -> dict[str, str]:
        return {"ok": "true"}

    @app.post("/v1/onboarding/telegram/install-link", response_model=InstallLinkResponse, response_model_by_alias=True)
    def create_install_link(body: InstallLinkRequest, x_api_key: str | None = Header(default=None)) -> InstallLinkResponse:
        if resolved_api_key and x_api_key != resolved_api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
        if not resolved_bot_username:
            raise HTTPException(status_code=503, detail="telegram global bot username is not configured")
        link = store.create_install_link(user_id=body.user_id, bot_username=resolved_bot_username)
        return InstallLinkResponse(dmUrl=link.dm_url, browserUrl=link.browser_url, expiresAt=link.expires_at)

    return app


app = create_app()
```

**step 4: verify**

```bash
cd tomo_core && uv run python -m unittest tests.test_control_api -v
```

expected: 1 passed.

### task 10: wire control api cli command

**objective:** run the control api locally and in deployment without custom shell glue.

**files:**
- modify: `tomo_core/src/tomo_core/cli.py`
- test: `tomo_core/tests/test_cli.py`

**step 1: write failing cli test**

```python
import unittest
from unittest.mock import patch

from tomo_core.cli import main


class CliTests(unittest.TestCase):
    def test_control_start_invokes_uvicorn(self):
        with patch("uvicorn.run") as run:
            code = main(["control", "start", "--host", "127.0.0.1", "--port", "9999"])
        self.assertEqual(code, 0)
        run.assert_called_once()
```

**step 2: implement parser branch**

add imports:

```python
import uvicorn
```

add subcommand near existing `telegram` subcommand:

```python
control = sub.add_parser("control", help="control api commands")
control_sub = control.add_subparsers(dest="control_command", required=True)
control_start = control_sub.add_parser("start", help="start control api")
control_start.add_argument("--host", default=os.getenv("TOMO_CONTROL_HOST", "127.0.0.1"))
control_start.add_argument("--port", type=int, default=int(os.getenv("TOMO_CONTROL_PORT", "8787")))
control_start.add_argument("--reload", action="store_true")
```

add branch before return:

```python
if args.command == "control" and args.control_command == "start":
    uvicorn.run("tomo_core.control_api:app", host=args.host, port=args.port, reload=args.reload)
    return 0
```

**step 3: verify**

```bash
cd tomo_core && uv run python -m unittest tests.test_cli -v
```

expected: cli test passes.

---

## phase 3: dashboard onboarding bff and cta

### task 11: add dashboard bff route for telegram onboarding

**objective:** authenticate the browser session, call the python control api, and redirect to telegram deeplink.

**files:**
- create: `dashboard/src/pages/_api/api/onboarding/telegram.ts`

**step 1: implement route**

```ts
import { auth } from '../../../../lib/auth';
import { dashboardEnv, requireDashboardEnv } from '../../../../lib/env';

export const GET = async (request: Request): Promise<Response> => {
  const session = await auth.api.getSession({ headers: request.headers });
  const url = new URL(request.url);

  if (!session?.user?.id) {
    const next = encodeURIComponent('/api/onboarding/telegram');
    return Response.redirect(`${url.origin}/login?next=${next}`, 302);
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
  if (!payload.dmUrl) {
    return new Response('telegram onboarding failed: missing telegram deeplink', { status: 502 });
  }
  return Response.redirect(payload.dmUrl, 302);
};
```

**step 2: verify unauthenticated redirect manually**

```bash
cd dashboard && bun run build
```

expected: route compiles.

manual after starting services:

```bash
curl -i http://localhost:3000/api/onboarding/telegram
```

expected: `302` to `/login?next=%2Fapi%2Fonboarding%2Ftelegram` when no session cookie.

### task 12: point landing cta at auth-first flow

**objective:** avoid unauthenticated `/api/onboarding/telegram` 500s and make “text tomo” always auth-first.

**files:**
- modify: `dashboard/src/components/landing-hero.tsx:31-35`

**step 1: update href only**

replace:

```tsx
href="/api/onboarding/telegram"
```

with:

```tsx
href="/login?next=/api/onboarding/telegram"
```

**step 2: verify visual copy unchanged**

```bash
cd dashboard && bun run build
```

expected: build passes; hero still shows existing cta text from `landing-content.ts`.

---

## phase 4: shared telegram gateway and per-user runtime instances

### task 13: create runtime instance factory

**objective:** make “one instance per user” explicit without creating multiple telegram pollers.

**files:**
- create: `tomo_core/src/tomo_core/instances.py`
- test: `tomo_core/tests/test_instances.py`

**step 1: write failing test**

```python
import tempfile
import unittest

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.providers import StaticProvider
from tomo_core.telegram import FakeTelegramClient


class RuntimeInstanceRegistryTests(unittest.TestCase):
    def test_same_tomo_id_returns_same_runtime_and_isolated_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = RuntimeInstanceRegistry(data_dir=tmp, provider_factory=lambda _tomo_id: StaticProvider("ok"), telegram_client=FakeTelegramClient())
            first = registry.get("tomo-a")
            second = registry.get("tomo-a")
            other = registry.get("tomo-b")

            self.assertIs(first, second)
            self.assertIsNot(first, other)
            self.assertIn("instances", str(first.config.data_dir))
            self.assertTrue(str(first.config.data_dir).endswith("tomo-a"))
```

**step 2: implement registry**

```python
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .models import RuntimeConfig
from .providers import ProviderAdapter
from .runtime import PersonalAgentRuntime
from .telegram import TelegramDeliverySink, TelegramClient


class RuntimeInstanceRegistry:
    def __init__(self, data_dir: str | Path, provider_factory: Callable[[str], ProviderAdapter], telegram_client: TelegramClient, soul_path: str = "SOUL.md") -> None:
        self.data_dir = Path(data_dir)
        self.provider_factory = provider_factory
        self.telegram_client = telegram_client
        self.soul_path = soul_path
        self._instances: dict[str, PersonalAgentRuntime] = {}

    def get(self, tomo_id: str) -> PersonalAgentRuntime:
        if tomo_id not in self._instances:
            instance_dir = self.data_dir / "instances" / tomo_id
            instance_dir.mkdir(parents=True, exist_ok=True)
            self._instances[tomo_id] = PersonalAgentRuntime(
                provider=self.provider_factory(tomo_id),
                telegram=TelegramDeliverySink(self.telegram_client),
                config=RuntimeConfig(data_dir=str(instance_dir), soul_path=self.soul_path),
            )
        return self._instances[tomo_id]
```

**step 3: verify**

```bash
cd tomo_core && uv run python -m unittest tests.test_instances -v
```

expected: 1 passed.

### task 14: implement shared telegram gateway routing

**objective:** route `/start <token>` to installation and all normal messages by `chat_id` to the correct runtime instance.

**files:**
- create: `tomo_core/src/tomo_core/shared_gateway.py`
- test: `tomo_core/tests/test_shared_gateway.py`

**step 1: write failing tests**

```python
import tempfile
import unittest

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.providers import StaticProvider
from tomo_core.shared_gateway import SharedTelegramGateway
from tomo_core.telegram import FakeTelegramClient


def private_update(text, chat_id="123", message_id=1):
    return {"update_id": message_id, "message": {"message_id": message_id, "from": {"id": int(chat_id)}, "chat": {"id": int(chat_id), "type": "private"}, "text": text}}


class SharedGatewayTests(unittest.TestCase):
    def test_start_token_binds_chat_and_sends_welcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient()
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link("user-1", "tmnvm_bot")
            registry = RuntimeInstanceRegistry(tmp, lambda _: StaticProvider("hello"), client)
            gateway = SharedTelegramGateway(client=client, store=store, instances=registry)

            gateway.process_update(private_update(f"/start {link.token}"))

            self.assertIsNotNone(store.installation_for_chat("123"))
            self.assertIn("connected", client.sent_messages[-1]["text"])

    def test_message_routes_to_bound_runtime_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeTelegramClient()
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link("user-1", "tmnvm_bot")
            installation = store.consume_start_token(link.token, chat_id="123", actor_id="123")
            registry = RuntimeInstanceRegistry(tmp, lambda _: StaticProvider("yooo from your own tomo"), client)
            gateway = SharedTelegramGateway(client=client, store=store, instances=registry)

            gateway.process_update(private_update("hi", chat_id="123", message_id=2))

            self.assertEqual(client.sent_messages[-1]["actor_id"], "123")
            self.assertIn("yooo", client.sent_messages[-1]["text"])
            self.assertTrue((__import__("pathlib").Path(tmp) / "instances" / installation.tomo_id / "sessions").exists())
```

**step 2: implement shared gateway**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .instances import RuntimeInstanceRegistry
from .models import InboundEnvelope
from .onboarding_store import TelegramOnboardingStore
from .telegram import TelegramClient


@dataclass
class SharedTelegramGateway:
    client: TelegramClient
    store: TelegramOnboardingStore
    instances: RuntimeInstanceRegistry

    def process_update(self, update: dict[str, Any]) -> bool:
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private":
            return False
        text = (message.get("text") or "").strip()
        chat_id = str(chat.get("id") or sender.get("id"))
        actor_id = str(sender.get("id") or chat_id)
        message_id = str(message.get("message_id"))

        if text.startswith("/start"):
            return self._handle_start(text=text, chat_id=chat_id, actor_id=actor_id, message_id=message_id)

        installation = self.store.installation_for_chat(chat_id)
        if installation is None:
            self.client.send_message(chat_id, "open tomo from the dashboard first, then press start here.", reply_to_message_id=message_id)
            return True

        runtime = self.instances.get(installation.tomo_id)
        runtime.handle_telegram_text(
            InboundEnvelope(
                connector="telegram",
                actor_id=chat_id,
                message_id=message_id,
                text=text,
                native_metadata={"chat_id": chat_id, "from_id": actor_id, "tomo_id": installation.tomo_id},
            )
        )
        return True

    def _handle_start(self, text: str, chat_id: str, actor_id: str, message_id: str) -> bool:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            self.client.send_message(chat_id, "open tomo from the dashboard so i can connect this chat.", reply_to_message_id=message_id)
            return True
        installation = self.store.consume_start_token(parts[1], chat_id=chat_id, actor_id=actor_id)
        if installation is None:
            self.client.send_message(chat_id, "that tomo link expired. tap text tomo on the dashboard again.", reply_to_message_id=message_id)
            return True
        self.instances.get(installation.tomo_id)
        self.client.send_message(chat_id, "tomo is connected. text me.", reply_to_message_id=message_id)
        return True
```

**step 3: verify**

```bash
cd tomo_core && uv run python -m unittest tests.test_shared_gateway -v
```

expected: 2 passed.

### task 15: add shared telegram polling command

**objective:** start exactly one shared poller for the global bot token.

**files:**
- modify: `tomo_core/src/tomo_core/cli.py`
- test: `tomo_core/tests/test_cli.py`

**step 1: extend cli tests**

add a parser smoke that verifies `telegram-shared start` refuses missing token:

```python
class CliTests(unittest.TestCase):
    def test_telegram_shared_start_requires_token(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["telegram-shared", "start"]), 2)
```

**step 2: add command**

add parser:

```python
shared = sub.add_parser("telegram-shared", help="shared hosted telegram gateway")
shared_sub = shared.add_subparsers(dest="shared_command", required=True)
shared_start = shared_sub.add_parser("start", help="start shared telegram polling")
shared_start.add_argument("--token", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"))
shared_start.add_argument("--bot-username", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME"))
shared_start.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
shared_start.add_argument("--soul", default=os.getenv("TOMO_CORE_SOUL", "SOUL.md"))
shared_start.add_argument("--model", default=os.getenv("TOMO_XAI_MODEL", "grok-composer-2.5-fast"))
shared_start.add_argument("--static-response", default=os.getenv("TOMO_CORE_STATIC_RESPONSE"))
shared_start.add_argument("--poll-timeout", type=int, default=30)
```

add branch:

```python
if args.command == "telegram-shared" and args.shared_command == "start":
    if not args.token:
        print("missing TOMO_TELEGRAM_GLOBAL_BOT_TOKEN or --token.", file=sys.stderr)
        return 2
    client = TelegramBotApiClient(token=args.token)
    oauth = build_oauth_manager(args)
    def provider_factory(_tomo_id: str):
        return build_provider(args, oauth)
    store = TelegramOnboardingStore(args.data_dir)
    instances = RuntimeInstanceRegistry(args.data_dir, provider_factory, client, soul_path=args.soul)
    gateway = SharedTelegramGateway(client=client, store=store, instances=instances)
    print("shared telegram gateway polling started. press ctrl+c to stop.")
    offset = None
    while True:
        for update in client.get_updates(offset=offset, timeout=args.poll_timeout):
            if "update_id" in update:
                offset = int(update["update_id"]) + 1
            gateway.process_update(update)
        time.sleep(0.2)
```

also import:

```python
import time
from .instances import RuntimeInstanceRegistry
from .onboarding_store import TelegramOnboardingStore
from .shared_gateway import SharedTelegramGateway
```

**step 3: verify focused cli tests**

```bash
cd tomo_core && uv run python -m unittest tests.test_cli -v
```

expected: tests pass without contacting telegram.

---

## phase 5: end-to-end validation and docs

### task 16: add local setup docs

**objective:** make the operator flow obvious and avoid env copy-paste slop.

**files:**
- create: `docs/onboarding-telegram.md`

**step 1: document env split**

```md
# telegram onboarding

## dashboard env

- `BETTER_AUTH_SECRET`: session signing secret, at least 32 chars.
- `BETTER_AUTH_URL`: dashboard public url, e.g. `http://localhost:3000`.
- `TOMO_DASHBOARD_DATA_DIR`: dashboard sqlite dir, default `.tomo_dashboard`.
- `TOMO_CONTROL_API_URL`: python control api origin, no `/v1` suffix.
- `TOMO_CONTROL_API_KEY`: shared secret sent as `x-api-key` to control api.

## control/shared gateway env

- `TOMO_CONTROL_API_KEY`: same value accepted by control api.
- `TOMO_CORE_DATA_DIR`: canonical python runtime data dir, default `.tomo_core`.
- `TOMO_TELEGRAM_GLOBAL_BOT_TOKEN`: one shared bot token.
- `TOMO_TELEGRAM_GLOBAL_BOT_USERNAME`: bot username without `@`.
- `XAI_API_KEY` or per-user supergrok oauth tokens for real model replies.

## local run

```bash
cd tomo_core
uv run tomo-core control start --host 127.0.0.1 --port 8787
uv run tomo-core telegram-shared start

cd ../dashboard
bun run dev
```

## flow

1. open dashboard.
2. click `text tomo`.
3. sign in or sign up.
4. telegram opens with the shared bot.
5. press start.
6. send a normal dm.

## invariants

- never run more than one shared gateway against the same global bot token.
- do not use the legacy `telegram start` command for the hosted global bot.
- a dashboard sign-in alone does not bind telegram. `/start <token>` does.
```

**step 2: verify docs path**

```bash
git status --short docs/onboarding-telegram.md
```

expected: file is untracked or modified only by this task.

### task 17: full automated test pass

**objective:** prove all new seams work without live telegram or live better auth network calls.

**files:**
- no source edits unless tests fail.

**step 1: run python focused tests**

```bash
cd tomo_core && uv run python -m unittest discover -s tests -v
```

expected: all python tests pass.

**step 2: run dashboard build**

```bash
cd dashboard && bun run typegen && bun run build
```

expected: waku build passes.

### task 18: local smoke with static provider

**objective:** verify the actual browser -> auth -> control -> telegram deeplink -> shared gateway path locally before claiming done.

**files:**
- no edits.

**step 1: start control api**

```bash
cd tomo_core
export TOMO_CORE_DATA_DIR="$PWD/.tomo_core"
export TOMO_CONTROL_API_KEY="dev-secret"
export TOMO_TELEGRAM_GLOBAL_BOT_USERNAME="<your_bot_username>"
uv run tomo-core control start --host 127.0.0.1 --port 8787
```

expected: `GET http://127.0.0.1:8787/v1/health` returns ok.

**step 2: start shared gateway in another terminal**

```bash
cd tomo_core
export TOMO_CORE_DATA_DIR="$PWD/.tomo_core"
export TOMO_TELEGRAM_GLOBAL_BOT_TOKEN="<bot_token>"
export TOMO_TELEGRAM_GLOBAL_BOT_USERNAME="<your_bot_username>"
export TOMO_CORE_STATIC_RESPONSE="yooo. your own tomo instance is alive."
uv run tomo-core telegram-shared start
```

expected: one poller starts. do not start any other poller for that token.

**step 3: start dashboard**

```bash
cd dashboard
export BETTER_AUTH_SECRET="$(openssl rand -base64 32)"
export BETTER_AUTH_URL="http://localhost:3000"
export TOMO_CONTROL_API_URL="http://127.0.0.1:8787"
export TOMO_CONTROL_API_KEY="dev-secret"
bun run dev
```

expected: dashboard starts.

**step 4: browser smoke**

- open `http://localhost:3000`.
- click `text tomo`.
- sign up or sign in.
- confirm telegram opens via `tg://resolve?...start=<token>`.
- press start.
- send `hi tomo`.

expected:
- telegram replies `tomo is connected. text me.` after `/start`.
- second dm gets static provider reply.
- `.tomo_core/onboarding.sqlite` has one installation row for the chat.
- `.tomo_core/instances/{tomo_id}/sessions/telegram_actor_{chat_id}.json` exists.

### task 19: production readiness checks

**objective:** catch the exact slop class this feature is vulnerable to.

**files:**
- no source edits unless checks expose issues.

**checks:**

```bash
# no secrets in output/contracts
grep -R "botToken\|TOMO_TELEGRAM_GLOBAL_BOT_TOKEN" -n dashboard/src tomo_core/src/tomo_core | cat

# only shared gateway should use getUpdates for global hosted flow
grep -R "get_updates\|getUpdates" -n tomo_core/src/tomo_core | cat

# verify cta auth-first
grep -R "api/onboarding/telegram" -n dashboard/src | cat
```

expected:
- dashboard never references bot token env.
- hosted docs/cta use auth-first flow.
- legacy `telegram start` still exists for dev but docs warn not to use it with global token.

---

## files likely to change

### dashboard

- `dashboard/package.json`
- `dashboard/bun.lock`
- `dashboard/src/lib/env.ts`
- `dashboard/src/lib/auth.ts`
- `dashboard/src/lib/auth-client.ts`
- `dashboard/src/pages/_api/api/auth/[...route].ts`
- `dashboard/src/pages/_api/api/onboarding/telegram.ts`
- `dashboard/src/components/login-form.tsx`
- `dashboard/src/pages/login.tsx`
- `dashboard/src/components/landing-hero.tsx`
- generated: `dashboard/src/pages.gen.ts` via `bun run typegen`

### python core

- `tomo_core/pyproject.toml`
- `tomo_core/src/tomo_core/onboarding_store.py`
- `tomo_core/src/tomo_core/control_api.py`
- `tomo_core/src/tomo_core/instances.py`
- `tomo_core/src/tomo_core/shared_gateway.py`
- `tomo_core/src/tomo_core/cli.py`
- `tomo_core/tests/test_onboarding_store.py`
- `tomo_core/tests/test_control_api.py`
- `tomo_core/tests/test_instances.py`
- `tomo_core/tests/test_shared_gateway.py`
- `tomo_core/tests/test_cli.py`

### docs

- `docs/onboarding-telegram.md`

---

## tests / validation summary

run before final handoff:

```bash
cd tomo_core && uv run python -m unittest discover -s tests -v
cd dashboard && bun run typegen && bun run build
```

live smoke with a real telegram bot token:

```bash
cd tomo_core && uv run tomo-core control start --host 127.0.0.1 --port 8787
cd tomo_core && uv run tomo-core telegram-shared start
cd dashboard && bun run dev
```

manual acceptance:

- unauthenticated `/api/onboarding/telegram` redirects to `/login?next=...`.
- authenticated onboarding route calls python control api and returns `302 tg://resolve?...start=...`.
- `/start <token>` binds chat exactly once.
- expired/reused token gets a helpful retry message.
- unknown chat gets told to start from dashboard.
- messages from two different telegram chat ids create two different `tomo_id` instance dirs.
- no dashboard response includes bot token or raw install-token db records.

---

## risks, tradeoffs, and open questions

- better auth with waku is supported, but waku route conventions are young. if `src/pages/_api/api/...` path differs in this app version, follow the better auth waku doc pattern and verify with `bun run typegen`.
- `better-sqlite3` is the recommended better auth sqlite driver. deployment must run dashboard on node-compatible infrastructure; do not assume bun runtime fixes native sqlite deployment.
- this plan implements in-process per-user runtime instances first. if hosted isolation must be daytona from day one, keep the same `RuntimeInstanceRegistry` interface but replace `PersonalAgentRuntime` creation with a `SandboxInstanceRegistry` adapter.
- current `InboundEnvelope` has no `room_id`; this is ok for dm-only v1. add `room_id` later before groups or multi-connector routing.
- install-link user id comes from better auth `session.user.id`. do not substitute email as the stable key.
- the dashboard auth db and python onboarding db are intentionally separate. the bridge is the authenticated bff call, not shared node/python table access.
- if the product literally requires better auth infrastructure hosted pages instead of in-app auth UI, keep the bff/control/shared-gateway design and replace only tasks 3-6 with the hosted provider flow.

---

## implementation order

1. dashboard deps + better auth route.
2. login page + auth-first cta.
3. python onboarding store.
4. python control api.
5. dashboard onboarding bff.
6. runtime instance registry.
7. shared telegram gateway.
8. cli wiring.
9. docs + automated tests.
10. live telegram smoke.
