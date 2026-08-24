# Tomo Cloudflare control plane

Worker + Durable Objects host for the packaged Python agent in a `lite` container.

This is the #16 cutover. Dashboard is not here. Railway is already paused. There is no webhook fallback.

## What it is

- Worker: Telegram webhook, `sendMessage`, image fetch, HMAC checks
- RegistryDO: allowlist + `actor_id → tomo_id` + chat bind
- OwnerDO: inbox, generation fence, burst debounce, cron rows, delivery leases, sandbox exec
- PeerDO: stub so peer routes do not 500
- `lite` Sandbox: `tomo-core sandbox-inbound` with OpenRouter
- R2: per-owner `tomo.sqlite3` checkpoints

## Secrets

Never commit these. Generate HMAC keys with `openssl rand -hex 32`.

```bash
cd cloudflare
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
npx wrangler secret put OPENROUTER_API_KEY
npx wrangler secret put CRON_CAPABILITY_KEY
npx wrangler secret put ATTACHMENT_CAPABILITY_KEY
npx wrangler secret put PEER_CAPABILITY_KEY
```

OpenRouter: set the key credit limit to $15/month in the OpenRouter dashboard.

R2 bucket `tomo-checkpoints`: enable object versioning, 7-day keep, in the Cloudflare dashboard.

## Allowlist

`wrangler.jsonc` `TOMO_ALLOWLIST` is seeded with the operator:

```json
[{"actor_id":"5995349219","tomo_id":"tomo-pandacover"}]
```

Add the other two later as more objects in that JSON, then `npx wrangler deploy`. No agent image rebuild. Their first DM binds.

## Deploy

Docker is required. Cloudflare builds the container from `tomo_core/Dockerfile.cloudflare`.

```bash
cd cloudflare
npm install
npm run types
npm run typecheck
npx wrangler r2 bucket create tomo-checkpoints
npm run deploy
```

Point Telegram at the Worker. Secret token must match `TELEGRAM_WEBHOOK_SECRET`.

```bash
curl --fail-with-body \
  --data-urlencode "url=https://tomo.<subdomain>.workers.dev/telegram/webhook" \
  --data-urlencode "secret_token=$TELEGRAM_WEBHOOK_SECRET" \
  "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook"
```

## Launch checks (#10)

On the operator Telegram chat:

1. A stranger is dropped
2. Operator gets a real reply
3. Two quick messages become one generation
4. A mid-turn message supersedes
5. A short delay cron fires once
6. A photo is understood, no image left in R2
7. Container is destroyed after the turn
8. Restore: copy a previous R2 object onto `owners/tomo-pandacover/tomo.sqlite3`, next turn has those memories
9. 402: temporary OpenRouter key with cap $0, Worker posts the budget one-liner, then put the real key back

## Restore

R2 key: `owners/<tomo_id>/tomo.sqlite3`. Copy a versioned object onto that key. Next turn hydrates `lite`. Do not snapshot OwnerDO.

## Models

- Agent: `deepseek/deepseek-v4-flash-0731`
- Vision: `meta/muse-spark-1.2-contributor` (contributor tier may train; slug is a later vars swap)
