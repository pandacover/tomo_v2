# Tomo Conversation Domain Glossary

This is the canonical vocabulary for Tomo's conversation domain. Terms describe product meaning and lifecycle boundaries, not storage classes, transport methods, or database details.

See the [user message → TurnRun lifecycle](docs/user-message-turnrun-lifecycle.html) for the end-to-end hosted Telegram flow.

## Identity and scope

- **owner**: the Tomo instance whose conversations, memories, and settings are being used. An owner is not a chat participant or connector account.
- **actor**: the participant who produced an inbound message on a connector.
- **connector**: a boundary that carries conversation events between Tomo and an external surface, such as Telegram.
- **session**: the ordered, owner-scoped record of accepted conversation activity for one conversational endpoint.
- **generation**: one candidate execution of a response to an input burst.
- **revision**: the monotonic ordering value that decides which generation currently owns a session.

Owner identity is `tomo_id`; a connector `actor_id` identifies a session endpoint and never authorizes another owner's data.

## Inbound conversation

- **inbound event**: a connector event offered to Tomo for durable acceptance and interpretation.
- **inbound message**: one accepted user-authored contribution with its connector provenance and ordering identity.
- **input burst**: one or more ordered inbound messages treated as a single conversational stimulus.
- **conversation situation**: the input burst, current session context, relevant personal context, visible partials, and Tomo's soul as understood for one TurnRun.
- **context hydration**: silent preparation of the conversation situation before model generation.
- **context snapshot**: the bounded, normalized context available to a TurnRun at a particular revision.
- **visible partial**: a frame from an interrupted generation whose delivery is confirmed or conservatively uncertain and therefore remains part of shared conversational context.
- **attachment**: connector-provenance media associated with one inbound message.
- **attachment resolution**: capability-scoped retrieval and normalization of attachment bytes before a segment begins.
- **vision observation**: bounded, untrusted image evidence supplied to the base segment; it is not a tool result or user-visible subagent turn.

## Turn reasoning and generation

- **TurnRun**: the complete response lifecycle initiated by one input burst, potentially spanning several model segments and tool rounds while remaining one logical assistant turn.
- **conversational move**: a social or cognitive purpose Tomo chooses next, not a wording template or delivery unit.
- **primary move**: the move that must make the TurnRun useful.
- **supporting move**: an optional ordered move that helps the primary move land naturally.
- **MovePlan**: the compact turn-level selection of moves, response goal, confidence, and optional reaction intent. It contains no chain-of-thought, does not determine frame count, and grants no tool or memory authority. The runtime preserves a canonical model plan, normalizes harmless advisory drift, or synthesizes a safe plan before dependent TurnRun events.
- **segment**: one continuous model generation between knowledge boundaries.
- **knowledge boundary**: the arrival of verified new information that may justify another segment, such as a tool observation, user interruption, approval, or external event.
- **provider stream**: the model's incremental output for one segment. Raw provider deltas are internal and are not conversation frames.
- **frame**: one complete, validated outward text unit emitted by a segment.
- **logical assistant turn**: the single assistant contribution represented by all accepted frames in a TurnRun, regardless of how many physical messages are delivered.

## Tools and controls

- **tool call**: a model request to invoke one bound capability with structured arguments.
- **tool batch**: independent, compatible tool calls requested by one segment and executed as one tool round.
- **tool observation**: the verified success or safe failure returned by a tool call for later reasoning.
- **unattended-safe tool**: a tool explicitly classified as safe for automation; unclassified tools are excluded from unattended execution.
- **approval-needed outcome**: an argument-free terminal automation result indicating that a known but blocked consequential tool requires user approval.
- **memory control**: a bounded intent to stage a memory, apply memory governance, or change an owner setting.
- **provisional memory**: generation-bound memory that cannot hydrate until its generation is accepted.
- **reaction intent**: a sparse, transient social intent that may become one best-effort connector reaction; it is never durable memory or a frame.
- **reply context**: a bounded immutable connector-supplied snapshot of the message an inbound user message explicitly replies to; it is quoted referent context, never a new instruction.
- **contract repair**: one bounded replacement generation after malformed pre-visible output. It is not a segment or a reflective agent loop.
- **peer Tomo**: a Tomo owned by a different human and addressed only through its public peer handle.
- **agent relationship**: an explicit, bilateral, revocable connection between two owners' Tomos.
- **relationship grant**: a directional, scoped, versioned permission within an agent relationship.
- **inter-agent thread**: a bounded sequence of peer requests between the same two related Tomos.
- **peer request**: an authenticated, idempotent request from one related Tomo to the other; it is evidence or a request, never owner authority.
- **commitment proposal**: a nonbinding peer suggestion that cannot commit either owner without the affected owner's exact confirmation.
- **pending peer confirmation**: a bounded, one-time owner decision for an exact disclosure or proposal that existing grants do not authorize.

## Delivery and completion

- **bubble**: a connector's physical rendering of a frame. On Telegram, one frame normally becomes one message bubble.
- **delivery reservation**: an idempotent claim that a particular frame may be sent once for the active generation.
- **delivery outcome**: the known state of a reserved frame: sent, suppressed, failed, or uncertain.
- **provisional frame**: a validated frame durably attached to its generation before external delivery is attempted.
- **accepted generation**: the generation whose completed contribution is admitted into canonical session history and whose provisional memories may become active.
- **supersession**: replacement of an active generation by a newer revision after new inbound conversation arrives.
- **fence**: an ownership check that prevents stale or superseded generations from writing state or performing connector side effects.
- **partial completion**: a terminal TurnRun that preserves already validated visible frames but cannot honestly produce a normal complete ending.
- **failed segment**: an attempted segment that accepts no frame or tool batch before terminal failure.
- **partial segment**: a segment that accepts at least one frame before terminal failure.

## Personal context

- **personal data repository**: the owner-scoped boundary for sessions, memories, settings, and search, independent of a particular storage mechanism.
- **autonomous personal memory**: provenance-bearing information Tomo may retain for an owner, excluding credentials and authentication secrets.
- **epistemic kind**: how a memory claim is qualified by origin, such as user-stated, session-derived, tool-derived, assistant conclusion, or inference.
- **surface scope**: whether memory is automatically always available, contextually retrieved, or available only through explicit search.
- **session search**: bounded owner-scoped retrieval of accepted conversation messages and their local context.

## Scheduled agency

- **cron job**: an owner-scoped, durable intention for Tomo to act on a schedule. It is a continuing product commitment, not a sandbox process or frozen notification string.
- **job intent**: the goal and constraints Tomo should interpret whenever a cron job becomes due.
- **schedule**: the temporal rule that determines when a cron job becomes due.
- **job run**: one immutable occurrence of a due cron job, with its own outcome and delivery lifecycle.
- **agent job**: a cron job whose due runs wake Tomo with fresh context so it can reason and use allowed capabilities before responding.
- **control notice**: a deterministic operational message used when scheduled agency cannot produce a normal response, such as terminal execution failure or required approval.
- **job lifecycle**: the rules governing whether a cron job remains active, pauses, ends after a bound, is cancelled, or self-destructs after terminal handling.
- **delivery destination**: the connector endpoint to which a job run's user-visible result or lifecycle message is addressed.
- **connection**: an owner-authorized external source or capability, such as Notion, that Tomo may consult when executing a job.
- **connected fact**: bounded, structured automation context that retains its value, source, observation time, freshness, and uncertainty.
- **job progress**: operational continuity across runs, such as previous outcome and completed-run count. Domain facts such as medication inventory remain personal context or connection data rather than cron-owned truth.

## Core invariants

```text
TurnRun -> Segment -> Frame -> Bubble
move != frame
segment != bubble
tool call != visible announcement
model plan is advisory; runtime MovePlan is required
new segment requires a knowledge boundary
attachment resolution is a pre-segment knowledge boundary
physical cancellation is best effort; fences are authoritative
```

## Peer exchange invariant

A peer Tomo can request or disclose, but cannot grant authority for either human owner. Peer relationships do not merge owners, sessions, files, attachments, memories, credentials, accounts, tools, or sandboxes.

## Runtime identity and storage

`RuntimeConfig.owner_id` defaults to `local` only for local direct use. Hosted
sandbox entry requires `TOMO_INSTANCE_ID`; it fails with a safe protocol error
when absent. Runtime and CLI data directories use `TOMO_CORE_DATA_DIR` or an
explicit `--data-dir`, defaulting to `.tomo_core` relative to the current
working directory; the SQLite file is `tomo.sqlite3` in that directory.
