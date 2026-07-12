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

`PersonalAgentRuntime` continues to own transport, current session loading and saving, and delivery. The conversation module has no connector, session-store, tool, or hosted-infrastructure dependencies.

Explicitly deferred:

- durable memory, retrieval, Dream, embeddings, and memory curation;
- session identity/schema changes, atomic writes, idempotent persistence, and delivery-retry duplication;
- concrete durable-memory, cross-session, and location context providers;
- mutating tools, approvals, idempotent side effects, and action-result observation;
- durable background jobs and milestone notifications;
- group chat, `room_id`, other connectors, reactions, and typing refresh;
- Railway/Daytona deployment and immutable snapshot rollout, which require a separate user-approved operation.

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

## progressive Telegram turns

Telegram normal text uses an `InputBurst` rather than a flattened message. A burst contains ordered `msg_n` items with update IDs, Telegram message IDs, timestamps, and exact user text. The prompt renderer serializes those items as a deterministic user-role JSON payload, so text that looks like labels or JSON remains untrusted user content. Host-confirmed visible assistant partials are appended as assistant-role context, never interpolated into system text.

`ConversationEngine.respond_iter()` emits typed user-facing events for a
TurnRun: turn start, validated `FrameReady` events, then completion. These are
not token streams and do not expose hidden reasoning. The legacy `respond()`
API remains a compatibility wrapper for callers that still expect one completed
logical result.

Sandbox inbound requests remain protocol v2 during the staged rollout because
their `InputBurst` shape is unchanged. Sandbox event output is protocol v3; the
host accepts v2 and v3 inbound payloads and event streams while old snapshots
are still active.

Railway keeps one logical assistant turn even when it sends multiple Telegram bubbles. SQLite tracks burst revisions, active generations, and delivery reservations. A newer normal message supersedes the active generation immediately; already visible bubbles remain in Telegram and become visible context for the replacement generation. Stale completions and stale sends are fenced by generation ID and revision.

Context hydration produces no frame or visible execution telemetry. Concrete
durable-memory, prior-session, and location sources are deferred; when added,
they are silent by default and must carry provenance and freshness. Safe,
independent, bound read-only tool calls may be batched in one tool round only
when their arguments are resolved, none depends on another result, and their
parallel execution, approval, cancellation, and failure semantics are
compatible. Tool calls and observations remain internal unless a later
validated frame naturally communicates a verified result.

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
11. context hydration is silent, and concrete memory, session, and location providers are deferred.
12. side effects, approvals, and background jobs are deferred and must not be advertised as available.
13. progressive events contain only validated outward frames, never chain-of-thought, token deltas, move labels, tool arguments, or observations.
14. cancellation, persistence, generation/revision fencing, and pre-send delivery checks remain authoritative.
