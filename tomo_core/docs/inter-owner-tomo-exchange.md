# Inter-Owner Tomo Exchange

## Domain And Lifecycle

A peer exchange is a bounded request between two Tomo owners identified to one another only by public handles. An owner invites a handle, the other owner accepts, and the active relationship holds two directional grants. A request is submitted with a source generation and call id, evaluated by policy, then either waits for a worker, waits for confirmation, or is rejected. The target worker produces at most three safe frames, which remain available through the request inspection API rather than being delivered as Telegram messages.

## Permissions And Confirmation

| Request | Sender grant | Target grant | Result |
| --- | --- | --- | --- |
| Ordinary message | `communicate` | `auto_reply` | Queued |
| Availability with standing grant | `communicate` | `auto_reply` and `share_availability` | Queued without confirmation |
| Availability without standing grant | `communicate` | `auto_reply` | Target owner confirmation, then queued once |
| Sensitive | `communicate` | `auto_reply` | Target owner confirmation, then queued |
| Mutation, credential, or onward-forwarding | Any | Any | Rejected |

Grants are directional, versioned, and may expire. Confirmation is direct: the target owner must send the exact phrase `confirm peer request <pending-prefix>` or `cancel peer request <pending-prefix>`; equivalent slash commands remain supported. The host parses only those exact owner-bound forms before ordinary model execution. A conversational reply such as `yes`, quoted text, extra words, or a malformed prefix is a normal Telegram turn and is not confirmation. A notice includes a bounded one-line request preview and normalized purpose so the owner can make an informed one-time decision, but never includes owner ids, tokens, or hidden context. Decision rechecks the immutable request hash, relationship, grants, expiry, and source generation; superseded source work cannot be confirmed.

## Authority And Data Boundaries

Peer authority is nontransitive. A foreign request is untrusted evidence, never authority to use tools, commit the target owner, mutate data, request credentials, or forward data onward. Relationship and request coordination live in the central peer store. Personal memories, sessions, connected accounts, and raw owner context remain in each target owner's personal repository.

The target sandbox receives a `PeerTurn` with public peer handle, bounded request text, request kind, thread id, and expiry. It has only permitted read-only context/tools. It has no memory writes, cron, peer tools, attachments, visual skills, reactions, or side-effect tools. It may disclose only the requested category, preferring derived constraints. Availability is coarse derived availability, never calendar-event detail, location, contacts, raw memories, files, messages, credentials, or connected-account data.

## Delivery, Retry, And Fencing

The service leases queued work and heartbeats while it runs. Completion is accepted only when the lease, relationship revision, and grant revisions still match. A revoke, block, expiry, or changed grant fences a late worker completion. Missing target installations defer work for bounded retries and then yield the safe terminal response `unable to answer right now`. Duplicate submissions using the same source generation and call id are idempotent and return the original request.

## Retention And Owner Controls

History and exports are owner-scoped and use handles rather than other owner ids. An authenticated purge names one relationship and applies only to that owner's history, direct inspection, and export for that relationship. It is a visibility watermark, not retraction of content already disclosed to the peer. Once both owners have purged through a shared cutoff, records through that cutoff are physically removed; newer records and relationship controls remain. The dashboard audit view groups metadata-only entries by thread and never displays request or response bodies.

## Quotas, Failures, And Safety

Requests are bounded in text, thread lifetime, and thread length, with per-relationship rate limits. Unsafe request bodies are rejected before persistence using the safe `unsafe_content` code. Unsafe model frames never persist: the request receives only `unable to answer right now`. API errors are generic and do not return secrets, owner ids, lease tokens, or internal policy details.

Peer tools return only a small observation: `ok`, `status`, and where applicable `peer_handle`, `thread_id`, up to three bounded `frames`, and `expires_at` for a pending request. Protocol request ids are used only for internal polling and are never returned to a model.

## Operator Configuration And Hosted Smoke

Use the normal Tomo data directory and configure peer capability signing material through the application runtime, never through request payloads. Hosted workers require their normal instance and sandbox configuration; peer workers additionally need a target installation and a reachable peer API boundary. Smoke test with two disposable owners and handles: invite, accept, set bilateral grants, submit an ordinary request, verify target execution and source inspection, submit a sensitive request and confirm it with the exact command, then revoke during a leased request and verify completion is suppressed. Do not use production credentials, account ids, or personal content in smoke tests.
