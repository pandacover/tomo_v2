# Long-turn recovery

Cloudflare turns no longer depend on one in-memory Durable Object invocation consuming an `execStream()` response until the model exits.

## Runtime ownership

1. OwnerDO persists the generation, deterministic sandbox process ID, log cursor, poll deadline, and next poll time before starting external work.
2. The sandbox starts `tomo-core sandbox-inbound` as a background process with `autoCleanup: false` and keep-alive enabled.
3. OwnerDO alarms fetch the accumulated process log, consume only bytes after the persisted cursor, preserve an incomplete line as durable carry, and deliver validated frames using the existing generation and delivery fences.
4. A terminal process is checkpointed to R2 and settled. A missing process, expired deadline, or five consecutive polling failures settles to a stable failure instead of leaving an active generation stuck forever.
5. Superseding input marks the generation inactive first and destroys the deterministic sandbox, so later polls cannot deliver or checkpoint it.

This design survives Durable Object eviction or a Worker deployment while the sandbox process is still running. The next alarm reconstructs all required state from SQLite and cumulative sandbox logs. Delivery reservations keep replayed log records idempotent.

## Deliberate recovery boundary

If the sandbox container itself disappears, Tomo does not replay the model/tool turn because replay could duplicate an external side effect. It fails the generation with `sandbox_process_missing`, clears the active turn, and asks the user to retry. Durable Object interruption is resumable; container loss is safe and convergent.

## Verification

- Unit coverage proves a partial log line can cross poll/eviction boundaries without replaying completed lines.
- Existing generation and delivery primary-key fences still suppress stale and duplicate frames.
- The live interruption drill should start an image-heavy or tool-heavy turn, redeploy the Worker while it is running, and confirm later frames arrive once, the generation settles, and the final R2 checkpoint is readable.

The live maintenance drill remains the release gate for issue #27; unit and deployment checks cannot force a production host eviction.
