# Conversation Architecture

## scope and architectural decisions

Tomo replies through a soul-aware TurnRun: interpret the situation, select compact turn-level intent, generate validated outward frames from continuous model segments, and return compact metadata.

In scope:

- define the situation, move, move plan, TurnRun, segment, frame, bubble, and result domain language;
- mold every move's objective and procedure to `SOUL.md`;
- select one primary move and zero to two ordered supporting moves;
- generate one to three intentional frames per segment with at most three sentences and the configured character cap per frame;
- keep move labels and reasoning internal while persisting compact move metadata;
- fall back to a safe direct-answer plan after malformed selection output;
- allow one undelivered contract-repair generation after malformed first-segment output;
- preserve one logical assistant turn even when Telegram receives several bubbles.

`PersonalAgentRuntime` owns transport and delivery while `PersonalDataRepository` is the only runtime boundary for owner-scoped sessions, memories, and settings. SQLite is its first adapter; SQL, FTS, and connection mechanics do not enter conversation code. The owner is the Tomo instance ID, never a connector actor ID. Local direct use defaults to owner `local`; hosted sandbox use requires `TOMO_INSTANCE_ID`. SQLite is authoritative; legacy JSON session files are not read.

Explicitly deferred:

- mutating tools, approvals, idempotent side effects, and action-result observation;
- durable background jobs and milestone notifications;
- group chat, `room_id`, other connectors, and typing refresh;
- Railway/Daytona deployment and immutable snapshot rollout, which require a separate user-approved operation.
- dashboard UI for personal-data settings and application-managed encryption.

## canonical terms

- conversation situation: the current input burst, silently hydrated context, and Tomo's soul.
- conversational move: the social or cognitive purpose Tomo chooses next, not a wording template.
- primary move: the main purpose that must make the turn useful.
- supporting move: an optional ordered move that helps the primary move land naturally.
- move plan: the compact, non-chain-of-thought turn-level decision containing moves, goal, and confidence; it does not determine frame or bubble count.
- TurnRun: the complete interaction initiated by one input burst, potentially spanning tools and several model segments.
- segment: one continuous model generation between knowledge boundaries.
- frame: one complete, validated outward text unit emitted by a segment.
- bubble: Telegram's delivery representation of a frame.
- tool batch: native tool calls requested by one segment and executed as one tool round.
- context snapshot: silently hydrated context available before the first segment.
- repair: one bounded replacement generation for malformed first-segment output before any frame or tool call, not a reflective agent loop.

## move catalog

| move | objective | procedure | completion |
|---|---|---|---|
| `acknowledge` | show precise attention without therapeutic warmth or fake excitement | identify what deserves recognition; react briefly; do not hijack the topic | the user can tell Tomo understood the important part |
| `answer` | give an honest, useful verdict rather than neutral assistant mush | answer first; support with strongest reasons; state uncertainty and practical consequence | the question is resolved as far as evidence allows |
| `clarify` | resolve material ambiguity without a customer-support interrogation | name what does not add up; explain why it matters; ask one sharp question | the minimum missing information has been requested |
| `explore` | follow an interesting unresolved thread with genuine curiosity | identify the loose end; connect context; ask one focused question and yield if declined | the user has a clear opening to deepen the thread |
| `challenge` | disagree plainly while staying accurate and useful | reconstruct the position; challenge the decision or reasoning, not vulnerability; offer a stronger route | the disagreement and better alternative are clear |
| `reassure` | reduce uncertainty with reality, not manufactured optimism | name the concern; separate danger from intensity; state what remains controllable | the user has a grounded picture and something useful to do |
| `joke` | build connection through observed absurdity or callbacks | find a real incongruity; keep it brief; return to substance | humor adds connection without replacing usefulness |
| `act` | move toward a real outcome without pretending work happened | confirm; identify prerequisites; execute only with capability and observed result | a real action is verified or the prerequisite is clear |
| `repair` | correct misunderstanding or bad tone without defensiveness | say `mb` when natural; identify the exact miss; correct it | shared understanding is restored |
| `refuse` | hold a real constraint without corporate policy speech | say no plainly; give the actual reason; offer the closest valid alternative | the limit and useful alternative are unambiguous |

## TurnRun model

```text
InboundEnvelope
  -> silent ContextHydration
  -> TurnRun
      -> select turn-level MovePlan
      -> Segment 0: one continuous model generation
          -> validate 1-3 Frames
          -> deliver each Frame as a Telegram Bubble
      -> optional later Segment only after verified new information
  -> persist one logical assistant turn + compact TurnRun metadata
```

Normal conversation uses exactly one model-generation segment. A segment may
emit multiple frames from the same provider stream; completed frames may be
delivered while that stream continues. Frames target one to two sentences and
are hard-limited to three sentences, three frames per segment, and
`RuntimeConfig.max_chars_per_frame`, whose accepted configurable default is
800 characters. This product limit is separate from Telegram's 4096-character
transport ceiling.

Another model generation requires genuinely new information: verified tool
observations, user interruption, approval, external events, or meaningful job
milestones. A next conversational move is not a knowledge boundary. One
separately counted contract-repair generation is allowed only when malformed
first-segment output yielded neither a deliverable frame nor a tool call; once
a frame is visible, malformed remainder completes as grounded partial output.

The model-facing plan is requested but advisory. The runtime always resolves a
MovePlan before exposing dependent TurnRun events: it preserves a canonical
model plan, normalizes harmless plan-field drift, synthesizes a direct-answer
plan when a Frame or memory control arrives first, or synthesizes an
`act`-then-`answer` plan after a planless native tool batch passes strict
preflight validation. A synthesized plan never guesses a reaction.

| Layer | Contract |
| --- | --- |
| Model-facing plan | SHOULD emit one canonical `turn_plan`; the runtime derives move order rather than accepting a model-authored sequence |
| Runtime plan | Required internally and classified as model-owned, normalized, or synthesized |
| Frames | Strict JSONL records with count, sentence, character, and style limits |
| Memory controls | Strict schema plus runtime governance; plan resolution grants no authority |
| Native tools | Strict availability, schema, budget, confirmation, preflight, and generation fencing |
| Malformed stream | Repair or fail; never recover records heuristically from prose or invalid JSON |

## progressive Telegram turns

Telegram normal text uses an `InputBurst` rather than a flattened message. A burst contains ordered `msg_n` items with update IDs, Telegram message IDs, timestamps, and exact user text. The prompt renderer serializes those items as a deterministic user-role JSON payload, so text that looks like labels or JSON remains untrusted user content. Host-confirmed visible assistant partials are appended as assistant-role context, never interpolated into system text.

`ConversationEngine.respond_iter()` emits typed user-facing events for a
TurnRun: turn start, validated `FrameReady` events, then completion. These are
not token streams and do not expose hidden reasoning. The legacy `respond()`
API remains a compatibility wrapper for callers that still expect one completed
logical result.

Sandbox inbound requests remain protocol v2 during the staged rollout because
their `InputBurst` shape is unchanged. Sandbox event output is protocol v3;
v3 permits at most one reaction event before frames. The host accepts v2 and
v3 inbound payloads and event streams while old snapshots are still active.

Railway keeps one logical assistant turn even when it sends multiple Telegram bubbles. SQLite tracks burst revisions, active generations, and delivery reservations. A newer normal message supersedes the active generation immediately; already visible bubbles remain in Telegram and become visible context for the replacement generation. Stale completions and stale sends are fenced by generation ID and revision.

Context hydration produces no frame or visible execution telemetry. Personal
memory has no semantic whitelist other than credentials and authentication
secrets; provenance captures uncertainty rather than blocking inference. FTS
tables are rebuildable indexes, prompt caps are not retention limits, and
reactions are delivery side effects rather than memories. Safe,
independent, bound read-only tool calls may be batched in one tool round only
when their arguments are resolved, none depends on another result, and their
parallel execution, approval, cancellation, and failure semantics are
compatible. Tool calls and observations remain internal unless a later
validated frame naturally communicates a verified result.

## inter-owner peer turns

Cross-owner communication does not reuse an ordinary owner turn. The broker
authenticates a source-generation capability, checks an explicit bilateral
agent relationship plus both directional grant revisions, and creates an
idempotent request in a bounded inter-agent thread. The recipient executes a
distinct `PeerTurn` in a relationship-scoped session. A peer message is
untrusted evidence or a request, never authority from the foreign owner.

`PeerTurn` runtime construction exposes only owner-bound, unattended,
read-only personal search. It excludes cron, recursive peer tools,
attachments, vision, reactions, memory controls, credentials, account
connectors, and every mutating tool. Completion remains fenced by the live
worker lease, relationship revision, grants, expiry, and service stop state.
Sensitive disclosures and availability outside a standing grant wait for one
exact owner confirmation. Deterministic host parsing accepts only `confirm
peer request <short-id>` or `cancel peer request <short-id>` (plus equivalent
slash commands); ambiguous language remains an ordinary conversation turn.
Relationship coordination is central, but owner memories, files, sessions,
accounts, credentials, and sandboxes never merge.

## personal-data lifecycle

The runtime persists inbound session data before generation, then accepts prior
generation IDs. It hydrates highest-salience `always` memory plus relevant
`contextual` memory as labeled user-trust data, subject to record and character
caps. `archive` memory is available only through `search_memories`; both
personal search tools are owner-scoped, read-only, and return no data when
retrieval is disabled.

The first plan may contain a sparse allowlisted reaction or `null`. After the
plan and leading owner-setting controls validate, the runtime rechecks
`reactions_enabled` and may emit one reaction before tools or frame 0. The
sandbox v3 event binds it to owner, actor, chat, generation, revision, and the
latest inbound message; the Telegram gateway fences stale revisions, dedupes a
delivery key, and isolates Telegram failures from frames. There is no hard
cooldown. Reaction intent is transient and is never stored as memory.

Autonomous memory controls stage generation-bound provisional rows during
segments. Only a later accepted generation activates them. User governance and
the three independent owner settings, `capture_enabled`, `retrieval_enabled`,
and `reactions_enabled`, apply immediately. All default to enabled. Capture
does not imply retrieval; retrieval does not imply reactions. Natural-language
forgetting disables exact owner-bound memory reversibly; permanent deletion
creates a ten-minute pending confirmation and does not infer confirmation from
an unrelated message. Session deletion is independent by default and marks
provenance unavailable; an explicit cascade option is required to remove
derived memories.

Accepted sessions and memories have no automatic expiry. Maintenance may prune
only stale provisional memories and expired pending deletion actions. FTS5 is
required at SQLite initialization. A missing FTS5 capability raises a safe
storage capability error; busy storage is surfaced as a safe storage error.
FTS indexes are rebuildable and are not canonical data.

## invariants

1. every normal reply has exactly one primary conversational move.
2. a turn has at most two supporting moves, in order.
3. moves are internal decisions; users receive only natural utterances.
4. move plans contain no chain-of-thought.
5. the authoritative hierarchy is `TurnRun -> Segment -> Frame -> Bubble`; move != frame and segment != bubble.
6. a normal reply uses one continuous model-generation segment and may contain 1-3 physical Telegram bubbles.
7. each frame has at most three sentences and the configured character cap; the accepted `max_chars_per_frame` default is 800, separate from Telegram's 4096-character transport limit.
8. the full SOUL.md shapes turn planning and segment generation.
9. a new model generation requires new information, except one undelivered contract repair.
10. no action is claimed without an observed result; tool call != visible announcement.
11. context hydration is silent; personal-memory and session-search providers are owner-bound, while location context remains deferred.
12. side effects, approvals, and background jobs are deferred and must not be advertised as available.
13. progressive events contain only validated outward frames, never chain-of-thought, token deltas, move labels, tool arguments, or observations.
14. cancellation, persistence, generation/revision fencing, and pre-send delivery checks remain authoritative.
15. the application persists no credential or authentication-secret memory, FTS content, logs, metrics, or sandbox completion summary.
16. local SQLite is not application-encrypted in v1; deployment isolation, restrictive permissions, encrypted volumes, backups, and encrypted transport are operational requirements.
