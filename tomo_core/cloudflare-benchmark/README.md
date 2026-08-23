# Tomo Cloudflare `lite` benchmark

This is a temporary, model-independent benchmark for migration issue 12. It runs the packaged Tomo Python runtime in three concurrent Cloudflare Sandbox instances using the `lite` container shape.

It does not implement the production control plane and does not call SuperGrok or another model. The fake provider reproduces delayed and streamed provider output. The image scenario runs Tomo's real Pillow normalization path eight times and persists the resulting observations through the real runtime.

## Scenarios

- `text`: real runtime, SQLite/FTS5 persistence, and streamed fake-provider output.
- `image`: eight sequential 4.9 MP image normalization and vision-observation passes.
- `image-max`: the same path at Tomo's 20 MP decoded-input limit.
- `wait`: a long provider wait; defaults to 120 seconds and accepts up to 720 seconds.

Every `/run` request creates fresh owner sandboxes, runs them concurrently, records process and cgroup memory, then destroys them to stop billing.

Set `"sleepCycle":true` to wait 35 seconds after the cold run and execute again with the same owner identifiers. This verifies the configured 30-second sleep and wake path before cleanup.

## Deploy

Docker is required because Wrangler builds the custom container image.

```bash
cd tomo_core/cloudflare-benchmark
npm install
npm run types
npm run typecheck
npx wrangler secret put BENCHMARK_TOKEN
npm run deploy
```

Use a strong random token. Do not put it in source, `wrangler.jsonc`, shell history, or an issue comment.

## Run

```bash
curl --fail-with-body \
  --request POST \
  --header "authorization: Bearer $TOMO_BENCHMARK_TOKEN" \
  --header "content-type: application/json" \
  --data '{"scenario":"text","owners":3,"delaySeconds":0.05}' \
  "https://tomo-cloudflare-lite-benchmark.<subdomain>.workers.dev/run"
```

Repeat with `image`, `image-max`, then with `wait` at 120 seconds. Run the 720-second wait only after the shorter scenarios fit comfortably.

For the sleep-and-wake check, send `{"scenario":"text","owners":3,"sleepCycle":true}`.

## Pass criteria

- All three owner commands succeed concurrently on `lite`.
- The cgroup peak stays below the 256 MiB limit with useful headroom.
- SQLite reports `integrity=ok`, FTS5 finds the persisted input, and protocol output completes.
- The normal and maximum image scenarios finish eight images without an OOM or invalid observation.
- Stream chunks arrive before command completion.
- The 120-second wait completes; the 720-second wait is a final duration proof, not the first test.

SuperGrok authentication and true provider latency remain a separate later validation.
