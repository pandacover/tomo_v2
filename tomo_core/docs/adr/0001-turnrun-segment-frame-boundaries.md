# ADR 0001: TurnRun Segment and Frame Boundaries

## Status

Accepted

## Context

The earlier progressive Telegram architecture realized each selected
`MovePlan` move with a separate model completion and mapped each utterance to
a Telegram bubble. Supplying previous utterances to later requests did not
make those requests one decoder trajectory, so a normal reply could read as
several independent speakers.

That plan remains authoritative for ordered input bursts, cancellation,
generation/revision fencing, durable delivery reservations, visible partial
replacement context, and one logical persisted assistant interaction. Its
"one move completion per utterance" realization rule is superseded by this
decision.

## Decision

The authoritative outward-response hierarchy is:

```text
TurnRun -> Segment -> Frame -> Bubble
```

- A `TurnRun` is the complete interaction initiated by one input burst. It
  owns the turn-level `MovePlan`, execution budgets, and logical persistence.
- A `Segment` is one continuous model generation between knowledge
  boundaries.
- A `Frame` is one complete, validated outward text unit from a segment.
- A `Bubble` is Telegram's delivery representation of a frame.
- A `MovePlan` is compact turn-level intent. It never determines the number
  of frames or bubbles: `move != frame` and `segment != bubble`.

Ordinary replies use exactly one normal model-generation segment. A segment
may emit one to three frames from the same provider stream, and completed
frames may be delivered while that stream continues. The target is one to two
sentences and one to two frames; three sentences and three frames are hard
maximums.

`RuntimeConfig.max_chars_per_frame` is a configurable product contract whose
accepted default is **800** characters. It is independent of Telegram's
4096-character text transport limit: 4096 remains a transport ceiling, not a
conversational-frame target or default.

Start a new normal model generation only when verified new information is
available, such as a tool observation, user interruption, approval, external
event, or meaningful job milestone. Generating the next conversational move
is not new information. The sole exception is one separately counted,
undelivered contract-repair generation (`max_contract_repairs=1`) when the
first segment produced neither a deliverable frame nor a tool call. Once any
frame is visible, malformed remainder ends as a grounded partial result rather
than a contradictory repair generation.

Context hydration is silent. Current history and host-confirmed visible
partials may be hydrated before the first segment without a frame or visible
tool-execution announcement. Future memory, prior-session, and location facts
are silent by default unless the user explicitly asks to consult them.

A segment may request one native `ToolBatch`. `tool call != visible
announcement`: short read-only work normally uses Telegram typing state and
does not generate mechanical start or finish bubbles. Calls may run
concurrently only when their arguments are resolved, none depends on another
call's output, every bound tool is read-only and parallel-safe, compatible
approval requirements hold, and cancellation/failure semantics are safe.
Observations are verified before a later segment consumes them.

An attempted generation with no accepted frame or tool batch is recorded as a
zero-output `FAILED` segment. A segment that emitted a validated frame before
failure is `PARTIAL`. This preserves honest usage accounting after visible
output while retaining pre-visible contract repair as a separate replacement
attempt rather than a segment. At a budget boundary after verified visible
output, a TurnRun may complete partially at a valid tool-batch boundary instead
of fabricating a final generation.

## Consequences

- Provider, conversation, runtime, sandbox, and delivery boundaries must carry
  frames without mandatory per-frame move metadata.
- Raw tokens, partial framing data, chain-of-thought, move labels, tool
  arguments, and tool observations never become visible events.
- A completed TurnRun persists as one logical assistant interaction even when
  it spans several segments and bubbles.
- Existing activity checks, pre-send delivery fencing, cancellation behavior,
  atomic persistence, and visible `sent`/`unknown` replacement context remain
  mandatory. Physical provider or worker cancellation is best effort; fences
  remain authoritative.
- The protocol migration may retain legacy v2 utterance reading during staged
  rollout, but new event semantics use frames and segment/frame coordinates.

## Deferred Work

This decision creates seams but does not provide concrete durable-memory,
cross-session, or location context providers. It also defers mutating tools,
approvals, idempotent side effects, durable background jobs, and background
milestone notifications. The runtime must not advertise those capabilities or
create placeholder tools for them.

## Migration Ordering

1. Add streaming/native tool-call transport characterization and select the
   frame cap in runtime configuration using the accepted default of 800.
2. Introduce TurnRun, Segment, Frame, budget, and framing contracts, then make
   ordinary replies one continuous segment.
3. Add silent current-context hydration and bound read-only tool batching.
4. Migrate persistence, sandbox events, and Railway delivery from utterances
   to frames while retaining the existing cancellation and fence guarantees.
5. Keep legacy protocol reading until all active sandboxes are migrated.

## Invariants

```text
move != frame
segment != bubble
tool call != visible announcement
new model generation requires new information, except one undelivered contract repair
```
