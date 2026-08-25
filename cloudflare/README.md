# Tomo Cloudflare control plane

Worker + Durable Objects host for the packaged Python agent in a `basic` container.

This is the #16 cutover. Dashboard is not here. Railway is already paused. There is no webhook fallback.

## What it is

- Worker: Telegram webhook, `sendMessage`, image fetch, HMAC checks
- RegistryDO: allowlist + `actor_id → tomo_id` + chat bind
- OwnerDO: inbox, generation fence, burst debounce, cron rows, delivery leases, sandbox exec
- PeerDO: stub so peer routes do not 500
- `basic` Sandbox: `tomo-core sandbox-inbound` with OpenRouter
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

## Checks

```bash
cd cloudflare
npm test
npm run types
npm run typecheck
```

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
7. A successful container stays reusable for two idle minutes, then sleeps; failed and superseded turns destroy it immediately
8. Restore: copy a previous R2 object onto `owners/tomo-pandacover/tomo.sqlite3`, next turn has those memories
9. 402: temporary OpenRouter key with cap $0, Worker posts the budget one-liner, then put the real key back

## Restore

R2 key: `owners/<tomo_id>/tomo.sqlite3`. Copy a versioned object onto that key. Next turn hydrates the guest. Do not snapshot OwnerDO.

## Container lifecycle and provenance

Production stays on `basic` (1 GiB memory). The earlier deterministic `lite` benchmark passed, but the complete live guest later produced container 500/OOM failures; reliability takes precedence over the smaller shape until a new live benchmark proves otherwise.

After a successful turn, the owner-keyed container remains available for a two-minute idle window. Follow-up messages reuse it without a cold start. It then sleeps and loses its ephemeral disk; the next turn restores SQLite from R2. Failures and superseded generations destroy immediately.

The deploy workflow writes the Git commit into the image and Worker vars, then calls the authenticated provenance probe. Deployment fails unless Worker and guest revisions match. Every owner also checks the image revision before hydration and destroys a stale guest. To roll forward after a failed provenance probe, rerun the same deploy; do not report it healthy from `/health` alone.

The `guest_ready` structured log records startup/readiness duration for each generation. Use it with `generation_done` to measure first-turn latency and failure rate without message contents.

At current Cloudflare rates, a two-minute idle tail on `basic` adds at most 0.03336 cents per successful turn before included usage. Three containers held active continuously would add about $21.64/month in provisioned memory and disk beyond included usage (plus active CPU), so this is a bounded reuse window, not a keep-warm service. At 3,000 successful turns/month, the idle tails are about $0.73 beyond included memory/disk usage, excluding actual turn runtime and CPU.

The guest uses direct HTTPS egress and the system CA store (`interceptHttps = false`). Container startup retries only failures that the Sandbox SDK classifies as transient, for three total attempts. Guest crashes and ordinary HTTP 500 errors are not replayed.

## Models

- Agent: `deepseek/deepseek-v4-flash-0731`
- Vision: `meta/muse-spark-1.2-contributor` (contributor tier may train; slug is a later vars swap)
