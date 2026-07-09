# telegram onboarding

## dashboard env

- `BETTER_AUTH_SECRET`: better auth session signing secret, at least 32 high-entropy chars.
- `BETTER_AUTH_URL`: dashboard public url, e.g. `http://localhost:3000` locally.
- `TOMO_DASHBOARD_DATA_DIR`: dashboard sqlite directory, default `.tomo_dashboard`.
- `TOMO_CONTROL_API_URL`: python control api origin, no `/v1` suffix.
- `TOMO_CONTROL_API_KEY`: shared secret sent from dashboard to control api as `x-api-key`.

## control and shared gateway env

- `TOMO_CONTROL_API_KEY`: same secret accepted by the control api.
- `TOMO_CORE_DATA_DIR`: canonical python runtime data dir, default `.tomo_core`.
- `TOMO_TELEGRAM_GLOBAL_BOT_TOKEN`: one shared bot token.
- `TOMO_TELEGRAM_GLOBAL_BOT_USERNAME`: bot username without `@`.
- `XAI_API_KEY` or per-user SuperGrok OAuth tokens for real model replies.

## local run

```bash
cd tomo_core
uv run tomo-core control start --host 127.0.0.1 --port 8787
uv run tomo-core telegram-shared start

cd ../dashboard
bun run dev
```

## railway core service

preferred: set the railway service root directory to `tomo_core/`. the committed `tomo_core/railway.toml` starts `scripts/railway_core_start.py`, which runs the control api on `$PORT` and starts the one shared telegram poller when `TOMO_TELEGRAM_GLOBAL_BOT_TOKEN` is set. the repo-root `railway.toml` is also present so a root-based core service has a start command instead of failing railpack detection.

mount a railway volume at `/data` and set:

```text
TOMO_CORE_DATA_DIR=/data
TOMO_CONTROL_API_KEY=<shared dashboard/control secret>
TOMO_TELEGRAM_GLOBAL_BOT_TOKEN=<botfather token>
TOMO_TELEGRAM_GLOBAL_BOT_USERNAME=<bot username without @>
XAI_API_KEY=<optional for real model replies>
```

health check path: `/v1/health`.

ops commands from `tomo_core/`:

```bash
uv run tomo-core telegram-shared start --background
uv run tomo-core telegram-shared stop
uv run tomo-core telegram-shared restart
```

railway uses `restart` to fire-and-forget the telegram poller, then execs the control api in the foreground for the service health check.

## flow

1. open the dashboard.
2. click `text tomo`.
3. sign in or sign up.
4. telegram opens with the shared bot.
5. press start.
6. send a normal dm.

## invariants

- never run more than one shared gateway against the same global bot token.
- do not use legacy `telegram start` for the hosted global bot.
- dashboard sign-in alone does not bind telegram. `/start <token>` does.
- the dashboard never sees or returns the telegram bot token.
- every bound chat routes to a per-user `tomo_id` runtime instance.
