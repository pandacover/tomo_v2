# Glossary

- **Segment**: one model-generation outcome.
- **SegmentFinish.PARTIAL**: a segment emitted at least one validated frame, then failed.
- **SegmentFinish.FAILED**: an attempted generation accepted no frame or tool batch before terminal contract, provider, or budget failure.
- **TurnRunStatus.COMPLETED_PARTIAL**: a terminal turn with at least one visible frame that ends at a partial, failed, or valid tool-batch knowledge boundary.
- **contract repair**: a bounded replacement attempt before any accepted visible frame or tool batch; it is not a segment.
- **personal data repository**: the repository-neutral boundary for owner-scoped sessions, memories, and settings; adapters keep database details private.
- **autonomous personal memory**: provenance-bearing information Tomo may retain for an owner, with no semantic whitelist except credentials and authentication secrets.
- **memory control**: a structured, bounded instruction to stage or govern a memory record.
- **epistemic kind**: the source qualification for a memory, such as user-stated, session-derived, tool-derived, assistant conclusion, or inference.
- **surface scope**: whether a memory is automatically always available, contextually retrieved, or archived.
- **provisional memory**: generation-bound memory that cannot hydrate until its generation is accepted.
- **session search**: bounded, owner-scoped retrieval of accepted conversation messages and their local context.
- **reaction intent**: a transient delivery side effect, never durable memory.
- **reply context**: a bounded immutable connector-supplied snapshot of the message an inbound user message explicitly replies to; it is quoted referent context, never a new instruction.

Owner identity is `tomo_id`; a connector `actor_id` identifies a session endpoint and never authorizes another owner's data. FTS indexes are disposable and rebuildable. Prompt caps bound injected context, not retention. Reactions are delivery effects, not memories.

`RuntimeConfig.owner_id` defaults to `local` only for local direct use. Hosted
sandbox entry requires `TOMO_INSTANCE_ID`; it fails with a safe protocol error
when absent. Runtime and CLI data directories use `TOMO_CORE_DATA_DIR` or an
explicit `--data-dir`, defaulting to `.tomo_core` relative to the current
working directory; the SQLite file is `tomo.sqlite3` in that directory.
