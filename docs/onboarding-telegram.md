# Telegram onboarding

## Dashboard setup

Configure the dashboard service with:

- `BETTER_AUTH_SECRET`: at least 32 high-entropy characters.
- `BETTER_AUTH_API_KEY`: required in production when using Better Auth Infrastructure.
- `BETTER_AUTH_URL`: public dashboard URL, such as `https://your-app.up.railway.app`.
- `TOMO_DASHBOARD_DATA_DIR`: SQLite directory; defaults to `.tomo_dashboard`.
- `TOMO_CONTROL_API_URL`: control API origin, without `/v1`.
- `TOMO_CONTROL_API_KEY`: shared secret sent as `x-api-key`.

For Better Auth Infrastructure ownership verification, use the deployed URL without a double slash, for example `https://your-app.up.railway.app/api/auth`. Redeploy the dashboard after changing `BETTER_AUTH_API_KEY` so `dash()` routes mount.

## User flow

1. The user signs in to the dashboard and selects **text tomo**.
2. The dashboard requests a short-lived Telegram deep link from the control API.
3. Telegram opens the one shared bot; the user sends `/start <token>` by pressing Start.
4. The listener consumes the one-time token, binds that private chat to one `tomo_id`, and provisions its runtime.
5. Later private DMs route to that same `tomo_id`.

The link expires after 10 minutes. Dashboard sign-in by itself does not bind a Telegram chat. A user whose link expires should select **text tomo** again.

## Local hosted mode

Run the shared gateway in-process for local development. `local` requires no Daytona variables.

```bash
cd tomo_core
export TOMO_HOSTED_RUNTIME=local
export TOMO_CORE_DATA_DIR=.tomo_core
export TOMO_TELEGRAM_GLOBAL_BOT_TOKEN='123:abc'
export TOMO_TELEGRAM_GLOBAL_BOT_USERNAME='your_bot'
export XAI_API_KEY='xai-api-key'
uv run tomo-core control start --host 127.0.0.1 --port 8787
```

In another terminal:

```bash
cd tomo_core
uv run tomo-core telegram-shared start
```

For a local static smoke test, set `TOMO_CORE_STATIC_RESPONSE`; it uses a fixed reply instead of model credentials. `telegram-shared start --background` is available locally and writes the PID and combined log at `<TOMO_CORE_DATA_DIR>/telegram_shared.pid` and `telegram_shared.log`. Stop or restart it with:

```bash
uv run tomo-core telegram-shared stop --data-dir .tomo_core
uv run tomo-core telegram-shared restart --data-dir .tomo_core
```

## Hosted invariants

- Run exactly one shared listener per global bot token. Use `telegram-shared`, never legacy `telegram start`, for the hosted bot.
- The listener accepts private chats only and verifies that the message sender is the bound actor.
- The dashboard never receives or returns the bot token.
- Delivery is durable at-least-once; a message can be repeated after a crash between execution/delivery and durable completion.
- In Daytona mode, Railway owns bot credentials and refresh credentials; sandboxes get only the current access token for a command.

See [Railway and Daytona operations](daytona-railway.md) for production variables, snapshot releases, logs, and recovery.
