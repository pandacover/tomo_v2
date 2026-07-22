# Portable Memory, Session Search, and Telegram Reactions Implementation Plan

> **For Hermes:** Load the `implement` and `opencode` skills, execute this plan task-by-task with TDD, delegate coding through the user's configured OpenCode variants, and independently verify every diff and test result.

**Goal:** Replace JSON session persistence with a portable repository-backed SQLite store, give Tomo autonomous and provenance-aware personal memory plus bounded memory/session retrieval, and allow sparse best-effort Telegram reactions to the triggering user message.

**Architecture:** Keep domain models, repository contracts, and runtime behavior independent of SQL dialect. The first production adapter uses Python `sqlite3` and owns every SQLite/FTS5 detail; future PostgreSQL/Supabase or MariaDB adapters implement the same contract and backend-specific search without changing conversation/runtime code. A single per-Tomo database stores normalized sessions, messages, unrestricted semantic memories, provenance, lifecycle state, and owner settings; FTS5 tables are disposable indexes. The same TurnRun agent emits bounded memory controls before visible frames, including after search tools, while generation acceptance fences autonomous writes. Reactions remain orthogonal gateway-owned side effects emitted after the initial plan and any leading owner-setting veto validate, but before tools or frame 0.

**Tech Stack:** Python 3.11, stdlib `sqlite3`, SQLite FTS5, dataclasses/Protocols, existing TurnRun JSONL framing, Telegram Bot API, `unittest`.

---

## Product decisions fixed by this plan

1. Tomo is an autonomous personal agent. Version 1 has **no semantic memory whitelist**: preferences, facts, people, relationships, projects, plans, commitments, events, routines, observations, tool-derived conclusions, and qualified inferences may all be remembered.
2. Credentials and authentication secrets are the one content exception. Passwords, API keys, OAuth tokens, recovery codes, private keys, and authentication answers must never enter canonical memory, FTS, logs, metrics, or protocol payloads.
3. Remembering does not certify truth. Every memory carries source kind, provenance, confidence, temporal metadata, salience, and `always | contextual | archive` surface scope. User-stated, session-derived, tool-derived, assistant-concluded, and inferred records remain distinguishable when hydrated.
4. The same TurnRun agent decides what to remember. Any model segment may emit bounded structured memory controls before that segment's first visible frame; later segments may therefore retain useful results from `search_sessions` or `search_memories` without a second extractor model.
5. New autonomous writes are immediately persisted as `provisional` and tied to `generation_id`. Acceptance promotes them to `active` or `archived`; cancelled/superseded generations never hydrate. No candidate/repetition threshold exists.
6. Direct user governance is different from autonomous remembering: reversible `disable_by_user` applies immediately and Tomo cannot undo it; permanent deletion requires a stored, exact-target confirmation flow. Deleted content may be learned again from future evidence.
7. Memory and session deletion are independent by default. A future dashboard may explicitly cascade from a session to memories learned from it, but deleting one never silently deletes the other.
8. Owner settings model capture and retrieval independently. A future dashboard may show one master switch first; dashboard UI is not part of v1. Natural-language inspect, correct, forget/disable, and confirmed delete are part of v1.
9. Memory hydration is automatic before segment 0: highest-salience `always` records plus FTS-relevant `contextual` records, bounded by configurable record/character caps. `archive` records require explicit `search_memories`. Prompt caps limit per-turn context, never stored subject matter.
10. Both `search_memories` and `search_sessions` are bound read-only tools. The model decides during segment 0 when history beyond automatic hydration is needed. Persistent connector threads are sessions; searches may span all connectors owned by the same `tomo_id`.
11. Accepted memories and accepted session history have indefinite default retention. Provisional assistant output and stale provisional memory controls are maintenance artifacts and may be pruned.
12. Memory/session context is injected as labeled **data at its original trust level**, never concatenated into privileged system/developer instructions. Search observations remain tool-role evidence.
13. Conversation durability is mandatory: inbound persistence failure prevents generation and assistant-frame persistence failure prevents frame release. Memory hydration, search, staging, and reaction failures degrade gracefully with internal diagnostics and no fabricated success.
14. Reactions are model-led, allowlisted, sparse, and best-effort with no hard cooldown. `null` is normal; owner opt-out is a deterministic veto. Only the latest message in a burst is eligible.
15. Reaction timing is exact: after the first `turn_plan` and any leading `set_owner_setting(reactions_enabled=False)` control validate, before tool execution and frame 0. The sandbox emits intent only; the Telegram-owning gateway rechecks generation/revision immediately before `setMessageReaction`.
16. V1 relies on per-user sandbox/volume isolation, restrictive file permissions, encrypted transport, and platform-managed encryption at rest. It does not add application-managed field encryption or SQLCipher.
17. SQLite is an adapter, not the application API. No `sqlite3.Connection`, SQL string, FTS `MATCH`, `bm25`, rowid, PRAGMA, or SQLite exception crosses the adapter boundary.
18. No SQLAlchemy is added. CRUD portability comes from repository contracts and canonical export/import; search remains adapter-specific. Future PostgreSQL/Supabase and MariaDB adapters run the same behavioral contract suite.

## Domain model

### Repository-neutral records

```python
@dataclass(frozen=True)
class SessionIdentity:
    id: str
    owner_id: str                 # tomo_id, not connector actor_id
    session_key: str
    connector: str
    actor_id: str

@dataclass(frozen=True)
class SessionSearchQuery:
    owner_id: str
    text: str
    limit: int = 5
    context_before: int = 2
    context_after: int = 2
    roles: tuple[str, ...] = ("user", "assistant")

@dataclass(frozen=True)
class SessionSearchHit:
    session_id: str
    session_key: str
    connector: str
    matched_message_id: str
    matched_role: str
    matched_text: str
    timestamp: str
    score: float                  # backend-neutral: larger is better
    context: tuple[StoredMessage, ...]

@dataclass(frozen=True)
class MemorySourceRef:
    source_kind: Literal["current_message", "session_message", "tool_observation", "assistant_conclusion", "inference"]
    source_id: str                 # message, observation, or generation/turn ID
    observed_at: str
    available: bool = True         # host-derived on read; omitted from model controls

@dataclass(frozen=True)
class MemoryWriteControl:
    action: Literal["upsert", "add", "remove", "archive", "disable_by_agent"]
    authority: Literal["autonomous", "explicit_user"]
    user_intent_excerpt: str | None
    memory_id: str | None
    kind: str
    subject_key: str
    topic: str
    value: object
    statement: str
    confidence: float
    salience: float
    surface_scope: Literal["always", "contextual", "archive"]
    valid_from: str | None
    valid_until: str | None
    sources: tuple[MemorySourceRef, ...]

@dataclass(frozen=True)
class MemoryGovernanceControl:
    action: Literal["disable_by_user", "request_delete"]
    target_memory_ids: tuple[str, ...]
    user_intent_excerpt: str

@dataclass(frozen=True)
class PendingMemoryActionControl:
    action: Literal["confirm_delete", "cancel_delete"]
    pending_action_id: str
    user_intent_excerpt: str

@dataclass(frozen=True)
class OwnerSettingControl:
    action: Literal["set_owner_setting"]
    setting: Literal["capture_enabled", "retrieval_enabled", "reactions_enabled"]
    enabled: bool
    user_intent_excerpt: str

MemoryControl = MemoryWriteControl | MemoryGovernanceControl | PendingMemoryActionControl | OwnerSettingControl
MemoryStatus = Literal["provisional", "active", "archived", "disabled_by_agent", "disabled_by_user", "superseded"]
MemoryEpistemicKind = Literal["user_stated", "session_derived", "tool_derived", "assistant_conclusion", "inferred"]

@dataclass(frozen=True)
class MemoryRecord:
    id: str
    owner_id: str
    kind: str
    subject_key: str
    topic: str
    value: object
    statement: str
    epistemic_kind: MemoryEpistemicKind
    status: MemoryStatus
    confidence: float
    salience: float
    surface_scope: Literal["always", "contextual", "archive"]
    valid_from: str | None
    valid_until: str | None
    sources: tuple[MemorySourceRef, ...]

@dataclass(frozen=True)
class MemoryContextQuery:
    owner_id: str
    text: str
    always_limit: int = 16
    contextual_limit: int = 8
    total_chars: int = 2500

@dataclass(frozen=True)
class MemorySearchQuery:
    owner_id: str
    text: str | None
    limit: int = 8

@dataclass(frozen=True)
class MemorySearchHit:
    memory: MemoryRecord
    score: float

@dataclass(frozen=True)
class MemoryGovernanceResult:
    outcome: Literal["applied", "pending_confirmation", "cancelled", "ambiguous", "not_found", "rejected"]
    target_memory_ids: tuple[str, ...] = ()
    pending_action_id: str | None = None

@dataclass(frozen=True)
class OwnerMemorySettings:
    owner_id: str
    capture_enabled: bool = True
    retrieval_enabled: bool = True
    reactions_enabled: bool = True
    governance_revision: int = 0
```

### Repository contract

Create one narrow application-facing protocol. Keep transaction and SQL mechanics private to adapters.

```python
class PersonalDataRepository(Protocol):
    def load_session(self, owner_id: str, session_key: str) -> ConversationSession: ...
    def save_session(self, owner_id: str, session: ConversationSession) -> None: ...
    def accept_generations(self, owner_id: str, session_key: str, generation_ids: tuple[str, ...]) -> None: ...
    def search_sessions(self, query: SessionSearchQuery) -> tuple[SessionSearchHit, ...]: ...
    def memory_context(self, query: MemoryContextQuery) -> tuple[MemoryRecord, ...]: ...
    def search_memories(self, query: MemorySearchQuery) -> tuple[MemorySearchHit, ...]: ...
    def stage_memory_controls(self, owner_id: str, session_key: str, generation_id: str, segment_index: int, governance_revision: int, controls: tuple[MemoryWriteControl, ...]) -> None: ...
    def apply_user_memory_control(self, owner_id: str, session_key: str, control: MemoryControl) -> MemoryGovernanceResult: ...
    def memory_settings(self, owner_id: str) -> OwnerMemorySettings: ...
    def update_memory_setting(self, owner_id: str, setting: str, enabled: bool) -> OwnerMemorySettings: ...
    def delete_session(self, owner_id: str, session_id: str, cascade_memories: bool = False) -> None: ...
    def delete_owner(self, owner_id: str) -> None: ...
```

`save_session` must preserve the current idempotency identities:

- user: `(session_id, burst_id, update_id)`;
- assistant: `(session_id, generation_id)`;
- fallback imported legacy row: stable generated message ID recorded during import.

## SQLite schema

Store one file at `<RuntimeConfig.data_dir>/tomo.sqlite3`.

```sql
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  session_key TEXT NOT NULL,
  connector TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(owner_id, session_key)
);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  ordinal INTEGER,
  connector_message_id TEXT,
  update_id INTEGER,
  burst_id TEXT,
  generation_id TEXT,
  generation_status TEXT CHECK(generation_status IS NULL OR generation_status IN ('provisional', 'accepted')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX messages_user_delivery_identity
  ON messages(session_id, burst_id, update_id)
  WHERE role = 'user' AND burst_id IS NOT NULL AND update_id IS NOT NULL;

CREATE UNIQUE INDEX messages_assistant_generation_identity
  ON messages(session_id, generation_id)
  WHERE role = 'assistant' AND generation_id IS NOT NULL;

CREATE TABLE accepted_generations (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  generation_id TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  PRIMARY KEY(session_id, generation_id)
);

CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject_key TEXT NOT NULL,
  topic TEXT NOT NULL,
  value_json TEXT NOT NULL,
  statement TEXT NOT NULL,
  search_text TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  epistemic_kind TEXT NOT NULL CHECK(epistemic_kind IN ('user_stated', 'session_derived', 'tool_derived', 'assistant_conclusion', 'inferred')),
  status TEXT NOT NULL CHECK(status IN ('provisional', 'active', 'archived', 'disabled_by_agent', 'disabled_by_user', 'superseded')),
  confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
  salience REAL NOT NULL CHECK(salience >= 0.0 AND salience <= 1.0),
  surface_scope TEXT NOT NULL CHECK(surface_scope IN ('always', 'contextual', 'archive')),
  source_generation_id TEXT,
  source_governance_revision INTEGER NOT NULL,
  supersedes_id TEXT REFERENCES memories(id) ON DELETE SET NULL,
  valid_from TEXT,
  valid_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX memories_owner_status_scope_salience
  ON memories(owner_id, status, surface_scope, salience, updated_at);

CREATE INDEX memories_owner_subject_topic
  ON memories(owner_id, subject_key, topic, updated_at);

CREATE INDEX memories_owner_fingerprint
  ON memories(owner_id, fingerprint, status);

CREATE TABLE memory_sources (
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  source_available INTEGER NOT NULL DEFAULT 1 CHECK(source_available IN (0, 1)),
  PRIMARY KEY(memory_id, source_kind, source_id)
);

CREATE TABLE owner_memory_settings (
  owner_id TEXT PRIMARY KEY,
  capture_enabled INTEGER NOT NULL DEFAULT 1 CHECK(capture_enabled IN (0, 1)),
  retrieval_enabled INTEGER NOT NULL DEFAULT 1 CHECK(retrieval_enabled IN (0, 1)),
  reactions_enabled INTEGER NOT NULL DEFAULT 1 CHECK(reactions_enabled IN (0, 1)),
  governance_revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

CREATE TABLE pending_memory_actions (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  session_key TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action = 'delete'),
  target_ids_json TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE memory_deletion_tombstones (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  deleted_at TEXT NOT NULL,
  request_key TEXT NOT NULL,
  UNIQUE(owner_id, request_key)
);

CREATE TABLE legacy_session_imports (
  source_path TEXT PRIMARY KEY,
  source_sha256 TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
  record_id UNINDEXED,
  owner_id UNINDEXED,
  content,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE memories_fts USING fts5(
  record_id UNINDEXED,
  owner_id UNINDEXED,
  search_text,
  tokenize='unicode61 remove_diacritics 2'
);
```

The SQLite adapter owns insert/update/delete triggers that mirror source rows into each FTS table by stable text `record_id`. Domain IDs never depend on FTS rowids. A tombstone contains no statement, value, topic, source text, fingerprint, or recoverable content. Every user governance mutation increments `governance_revision`; autonomous controls captured under an older revision cannot stage or activate. User-disabled rows remain stored but are excluded from hydration and agent search; a duplicate autonomous upsert resolves to the same disabled record and cannot reactivate it. Hard deletion removes the canonical row, provenance, FTS entry, and matching pre-delete provisional rows, allowing only genuinely later evidence to create a new memory.

Each connection must execute:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 5000;
```

Use one short-lived connection per repository operation and explicit transactions. Convert lock/busy failures into a repository-level `StorageBusyError`; do not leak `sqlite3.OperationalError` into runtime code.

## Search semantics

1. Normalize user queries into bounded terms; never pass arbitrary model text directly as raw FTS syntax.
2. Support quoted phrases and prefix matching through an adapter-private compiler.
3. Automatic memory hydration loads at most 16 highest-salience `always` rows plus 8 FTS-relevant `contextual` rows, deduped by stable ID and capped at 2,500 characters. Values are configurable and truncation occurs only at record boundaries.
4. `search_memories` searches `active` plus `archived` rows, never provisional, superseded, user-disabled, or agent-disabled rows. A blank query lists a bounded inventory for natural-language inspection.
5. Memory ranking is backend relevance first, then salience/confidence, with recency only as a tie-breaker. Reading a memory never increases salience automatically.
6. `search_sessions` queries all persistent connector threads owned by `tomo_id`, filters assistant rows to `generation_status = 'accepted'`, and may include current-thread history outside the normal context window.
7. Convert SQLite’s lower-is-better `bm25()` result into backend-neutral larger-is-better ordering; contract tests assert visibility/order, not exact cross-backend scores.
8. Fetch session context windows by message ordering within the same session. Default to 5 hits, 2 messages before/after, and 4,000 returned characters.
9. Never return metadata blobs, attachments, internal plans, credentials, or unbounded tool observations. Return provenance/source kind with memory hits and connector/timestamp/role with session hits.
10. When `retrieval_enabled` is false, automatic hydration and both agent search tools return no personal data. Capture remains independently controlled by `capture_enabled`.
11. If FTS fails, automatic hydration falls back to `always` rows and a bounded canonical-table search where possible; explicit tool searches return a typed error rather than fabricating results.
12. Future PostgreSQL can use `tsvector`/`websearch_to_tsquery`; MariaDB can use `MATCH ... AGAINST`; Supabase can expose Postgres RPC. Runtime and tool output remain unchanged.

---

### Task 1: Lock vocabulary and persistence boundaries

**Objective:** Document the new terms before code uses them inconsistently.

**Files:**
- Modify: `tomo_core/CONTEXT.md`
- Modify: `tomo_core/docs/conversation-architecture.md`
- Test: `tomo_core/tests/test_context.py`

**Steps:**
1. Add failing glossary assertions for “personal data repository,” “autonomous personal memory,” “memory control,” “epistemic kind,” “surface scope,” “provisional memory,” “session search,” and “reaction intent.”
2. Run: `UV_PROJECT_ENVIRONMENT=/tmp/tomo-v2-memory-venv uv run python -m unittest tests.test_context -v`
3. Define ownership: `owner_id == tomo_id`; connector `actor_id` identifies a session endpoint but never authorizes cross-owner reads.
4. State that memory has no semantic whitelist except credential/authentication-secret exclusion; provenance expresses trust and uncertainty rather than blocking inference.
5. State that FTS tables are rebuildable indexes, prompt caps are not retention limits, and reactions are delivery side effects rather than memories.
6. Re-run the test; expected: PASS.

### Task 2: Introduce repository-neutral models and contract

**Objective:** Make runtime persistence depend on domain behavior rather than JSON files or SQLite.

**Files:**
- Create: `tomo_core/src/tomo_core/personal_data.py`
- Modify: `tomo_core/src/tomo_core/sessions.py`
- Create: `tomo_core/tests/test_personal_data_contract.py`

**Steps:**
1. Write contract tests covering load-empty, idempotent inbound append, assistant generation dedupe, generation acceptance, owner isolation, unrestricted memory kinds, source provenance, provisional activation, archive/disable/supersede/delete, owner settings, both search APIs, independent/cascade session deletion, and owner deletion.
2. Add the records and `PersonalDataRepository` protocol shown above.
3. Keep `ConversationSession` and `StoredMessage` database-neutral in `sessions.py`; remove filesystem logic only after the SQLite adapter and importer pass.
4. Add an in-memory test adapter in the test module so contract semantics are executable independently of SQLite.
5. Run: `uv run python -m unittest tests.test_personal_data_contract -v`; expected: PASS.

### Task 3: Add versioned SQLite migrations

**Objective:** Create the normalized schema and FTS5 indexes transactionally.

**Files:**
- Create: `tomo_core/src/tomo_core/sqlite_personal_data.py`
- Create: `tomo_core/tests/test_sqlite_personal_data.py`

**Steps:**
1. Write failing tests for first initialization, idempotent reopening, migration rollback, foreign keys, WAL, busy timeout, every canonical/provenance/settings/pending-action/tombstone table, FTS5 availability failure, and schema version mismatch.
2. Implement an ordered migration list in Python; do not scatter `CREATE TABLE` statements through repository methods.
3. Apply each migration under `BEGIN IMMEDIATE`, recording `schema_migrations` only after all statements succeed.
4. Raise a clear `StorageCapabilityError("sqlite_fts5_unavailable")` if FTS5 cannot be created.
5. Run: `uv run python -m unittest tests.test_sqlite_personal_data -v`; expected: PASS.

### Task 4: Implement SQLite session persistence

**Objective:** Preserve current conversation and generation semantics in normalized rows.

**Files:**
- Modify: `tomo_core/src/tomo_core/sqlite_personal_data.py`
- Test: `tomo_core/tests/test_sqlite_personal_data.py`
- Modify: `tomo_core/tests/test_sessions.py`

**Steps:**
1. Add failing adapter-contract tests for concurrent writers, inbound dedupe, provisional assistant rows, accepted generation promotion, attachments preserved in `metadata_json`, and rollback on failure.
2. Implement `load_session`, `save_session`, and `accept_generations` with parameterized SQL only.
3. Preserve current `model_history_for_burst` behavior: exclude current burst’s inbound rows and unaccepted provisional assistant rows.
4. Test two repository instances writing the same DB without losing rows.
5. Run both session test modules; expected: PASS.

### Task 5: Implement FTS synchronization and session search

**Objective:** Search accepted session content and active/archive memory with bounded results and strict tenant scoping.

**Files:**
- Modify: `tomo_core/src/tomo_core/sqlite_personal_data.py`
- Test: `tomo_core/tests/test_sqlite_personal_data.py`

**Steps:**
1. Write failing tests for insertion, update, hard deletion, disable exclusion, archive search, Unicode text, phrases, prefixes, malformed FTS punctuation, ranking, salience tie-breaks, role filters, provisional assistant exclusion, context windows, result limits, settings-off behavior, and cross-owner leakage.
2. Add trigger-backed synchronization for `messages_fts` and `memories_fts`.
3. Implement a private `_compile_fts5_query(text: str) -> str` that tokenizes and quotes safe terms; reject blank queries before SQL.
4. Implement `memory_context`, `search_memories`, and `search_sessions`; blank memory inventory bypasses FTS, while blank session queries are rejected. Normalize scores so larger always means better without exposing backend score formulas.
5. Add an index rebuild test that deletes/recreates FTS rows from source tables without changing source data.
6. Run: `uv run python -m unittest tests.test_sqlite_personal_data -v`; expected: PASS.

### Task 6: Add portable export/import primitives

**Objective:** Prove data can leave SQLite without relying on SQLite `.dump` syntax.

**Files:**
- Create: `tomo_core/src/tomo_core/personal_data_transfer.py`
- Create: `tomo_core/tests/test_personal_data_transfer.py`

**Steps:**
1. Define a versioned canonical JSON Lines format containing sessions, messages, accepted generations, unrestricted memories, provenance links, owner settings, and content-free tombstones with stable text IDs and ISO timestamps; exclude FTS rows and expired pending confirmations.
2. Write a failing SQLite → canonical stream → in-memory adapter round-trip test.
3. Implement streaming export/import against repository methods, with owner scope and idempotent IDs.
4. Verify malformed versions and cross-owner records fail atomically.
5. Run: `uv run python -m unittest tests.test_personal_data_transfer -v`; expected: PASS.

This is the future migration path: implement a new adapter, run the same contract suite, export from SQLite, import into the new adapter, rebuild its search index, then switch configuration.

### Task 7: Import existing JSON sessions safely

**Objective:** Migrate existing users without deleting or duplicating session history.

**Files:**
- Create: `tomo_core/src/tomo_core/legacy_session_import.py`
- Create: `tomo_core/tests/test_legacy_session_import.py`
- Modify: `tomo_core/src/tomo_core/sessions.py`

**Steps:**
1. Write fixtures for normal, recovery-copy, corrupt, duplicate, provisional, and accepted-generation JSON sessions.
2. Import all `data_dir/sessions/*.json` in one file-scoped transaction and record path + SHA-256 in `legacy_session_imports`.
3. Map imported assistant generation status using `accepted_generation_ids`; preserve all metadata JSON.
4. Leave legacy files untouched as rollback evidence. Runtime must stop writing them after cutover.
5. On a changed file with an already-recorded path, fail visibly rather than silently merge unknown state.
6. Run: `uv run python -m unittest tests.test_legacy_session_import -v`; expected: PASS.

### Task 8: Wire owner identity and repository construction through every runtime path

**Objective:** Make local, in-process hosted, and Daytona sandbox runtimes use the same database contract.

**Files:**
- Modify: `tomo_core/src/tomo_core/models.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/instances.py`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py`
- Modify: `tomo_core/src/tomo_core/cli.py`
- Modify: `tomo_core/tests/test_instances.py`
- Modify: `tomo_core/tests/test_runtime_conversation_moves.py`
- Modify: `tomo_core/tests/test_sandbox_inbound.py`

**Steps:**
1. Add failing tests showing `RuntimeConfig.owner_id` is required for persisted/searchable state and defaults safely only in explicit single-user local mode.
2. Add injectable `personal_data_repository` to `PersonalAgentRuntime`; default it to `SqlitePersonalDataRepository(data_dir / "tomo.sqlite3")`.
3. Pass `tomo_id` from `RuntimeInstanceRegistry.get(tomo_id)` into `RuntimeConfig.owner_id`.
4. In `sandbox-inbound`, pass `TOMO_INSTANCE_ID` into `RuntimeConfig.owner_id`; fail with a safe protocol error if hosted mode lacks it.
5. Run legacy import once during repository initialization before loading a session.
6. Replace all `JsonSessionStore` production uses; retain it only as import compatibility until a later cleanup release.
7. Run the listed tests; expected: PASS.

### Task 9: Add structured memory controls to every model segment

**Objective:** Let the same TurnRun agent autonomously stage bounded memories from initial context or later tool results without an extractor call.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/src/tomo_core/conversation/framing.py`
- Modify: `tomo_core/tests/test_conversation_models.py`
- Modify: `tomo_core/tests/test_conversation_parsing.py`
- Modify: `tomo_core/tests/test_conversation_prompts.py`
- Modify: `tomo_core/tests/test_conversation_framing.py`

**Steps:**
1. Extend `SegmentFrameParser` to accept zero or more `memory_control` JSONL records after segment 0's `turn_plan` and before that segment's first `frame`; reject controls after visible output.
2. Add `MemoryControlReady(segment_index, control)` to conversation events and carry validated controls in `SegmentResult` for audit without placing their content in public sandbox completion summaries.
3. Bound controls to 5 per segment and 8 per TurnRun; parse the discriminated control variants above. Validate exact keys, compact `kind/subject_key/topic`, JSON value size, statement length, confidence/salience ranges, temporal fields, source count, and `always | contextual | archive` scope only where that variant permits them.
4. Permit source references only to current inbound message IDs, owner-bound session-search message IDs, current tool-observation IDs, or an explicit inference/assistant-conclusion marker. Reject foreign, missing, or unobserved source IDs.
5. Add a credential/authentication-secret detector at the runtime validation boundary. Reject the individual control with a typed internal diagnostic without logging its statement/value and continue the turn.
6. Prompt Tomo to remember autonomously when future usefulness justifies it, with no semantic category allowlist. Require epistemic qualification rather than suppressing uncertain observations.
7. Keep memory controls out of system messages, Telegram output, ordinary tool observations, logs, metrics, and `_safe_result` protocol summaries.
8. Test initial-context controls, controls after `search_sessions`, controls after `search_memories`, multiple segments, control-after-frame rejection, per-turn bounds, malformed sources, credential rejection, and no extra provider call.
9. Run the listed tests; expected: PASS.

Example bounded plan record:

```json
{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"answer directly","confidence":"high","reaction":null}
{"type":"memory_control","action":"upsert","authority":"autonomous","user_intent_excerpt":null,"memory_id":null,"kind":"project_context","subject_key":"self","topic":"projects.tomo.identity","value":{"description":"autonomous personal agent"},"statement":"Tomo is intended to be an autonomous personal agent","confidence":1.0,"salience":0.96,"surface_scope":"always","valid_from":null,"valid_until":null,"sources":[{"source_kind":"current_message","source_id":"msg_123","observed_at":"2026-07-13T00:00:00Z"}]}
```

### Task 10: Stage and activate autonomous memories transactionally

**Objective:** Prevent failed or superseded generations from mutating active memory.

**Files:**
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/sqlite_personal_data.py`
- Modify: `tomo_core/tests/test_runtime_conversation_moves.py`
- Modify: `tomo_core/tests/test_sqlite_personal_data.py`

**Steps:**
1. Write failing tests for arbitrary memory kinds, all epistemic/source kinds, controls in multiple segments, immediate provisional persistence, cancellation before/after frames, completed-but-unaccepted generations, later acceptance, archive scope, temporal facts, duplicate fingerprints, and exact-subject/topic correction chains.
2. On `MemoryControlReady`, validate ownership and write autonomous `upsert/add/remove/archive/disable_by_agent` changes immediately as generation-bound `provisional` rows. Memory-write failure records a content-free diagnostic and does not abort frames.
3. At the beginning of the next burst, promote controls for accepted generation IDs in the same transaction as assistant-message acceptance. `always/contextual` promotes to `active`; `archive` promotes to `archived`.
4. Derive the row's summary epistemic kind from source references using the most qualification-demanding source (`inferred` > `assistant_conclusion` > `tool_derived` > `session_derived` > `user_stated`). Preserve every provenance row so mixed evidence remains inspectable.
5. Implement `upsert/add/remove` semantics by `(owner_id, subject_key, topic, normalized value)`: exact corrections may supersede; additive values coexist; removals close/supersede the matching assertion. Unknown conflicts coexist with confidence/temporal qualifiers rather than invoking a candidate gate.
6. Never let an autonomous control reactivate `disabled_by_user`; duplicate fingerprints attach no new visible row. Agent-disabled/archive records may be revised or reactivated by later accepted controls.
7. Capture the owner's `governance_revision` at turn start. Reject staging or activation when it no longer matches. Honor `capture_enabled = false` by ignoring `authority = autonomous`; permit `authority = explicit_user` only for an exact owner-bound existing memory ID plus an exact current-burst intent excerpt, so correction/removal of existing data remains possible without silently enabling new capture.
8. Prune stale provisional rows only in maintenance, never during a user turn; retain accepted memories and sessions indefinitely.
9. Run the listed tests; expected: PASS.

### Task 11: Hydrate bounded autonomous memory before segment 0

**Objective:** Surface agent-selected always memory and contextually relevant memory every turn without prompt-privilege escalation or context flooding.

**Files:**
- Modify: `tomo_core/src/tomo_core/context.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/tests/test_context.py`
- Modify: `tomo_core/tests/test_conversation_prompts.py`

**Steps:**
1. Write a sentinel test proving memory statements/values never appear in system-role content and preserve explicit source/epistemic labels in the data block.
2. Before building segment 0, call `memory_context` with the complete current burst. Select up to 16 highest-salience `always` rows plus 8 FTS-relevant `contextual` rows, dedupe by ID, and cap at 2,500 characters at record boundaries.
3. Serialize memory as a labeled user-trust data message containing memory ID, statement/value, epistemic kind, confidence, temporal validity, and source label. Never present an inference as a user-stated fact.
4. Exclude provisional, superseded, archived, agent-disabled, and user-disabled rows. When `retrieval_enabled` is false, inject no memory block and expose no searchable memory data.
5. Keep trusted behavioral instructions static in the system prompt; test malicious-looking stored text cannot introduce provider roles, tool schemas, or instructions.
6. Test FTS failure fallback to bounded `always` memory, prompt caps, no partial-record truncation, no salience mutation on read, and graceful memory-store failure with intact session context.
7. Run the listed tests; expected: PASS.

### Task 12: Add bound read-only memory and session search tools

**Objective:** Let Tomo explicitly recover omitted/archive memories or raw historical conversation when automatic context is insufficient.

**Files:**
- Create: `tomo_core/src/tomo_core/personal_search_tools.py`
- Modify: `tomo_core/src/tomo_core/tools.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/instances.py`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py`
- Create: `tomo_core/tests/test_personal_search_tools.py`
- Modify: `tomo_core/tests/test_turn_run_tools.py`
- Modify: `tomo_core/tests/test_instances.py`
- Modify: `tomo_core/tests/test_sandbox_inbound.py`

**Steps:**
1. Write failing tests for both schemas, bound owner scope, cross-connector session search, blank memory inventory, malformed query handling, status filtering, result truncation, read-only/parallel-safe flags, settings-off behavior, and actual availability in local and sandbox runtimes.
2. Expose `search_memories(query?, limit?)` and `search_sessions(query, limit?)`. Never expose `owner_id`, SQL, table names, raw backend filters, or database paths.
3. Return stable memory JSON with ID, statement/value, source/epistemic kind, confidence, salience, scope, and temporal validity. Return session JSON with connector/thread, timestamp, role/text, score, and compact neighboring window.
4. Update prompt guidance: call `search_memories` for normalized personal context omitted from hydration; call `search_sessions` for provenance, chronology, prior discussion detail, or material never promoted. Permit one reformulation after zero results and prohibit visible frames before required retrieval completes.
5. Register both tools through the actual runtime registry in `PersonalAgentRuntime`, `RuntimeInstanceRegistry`, and `sandbox_inbound.build_runtime`; do not merely add them to a generic list.
6. Ensure providers without native tool calls fail fast when this nonempty registry is bound, preserving the existing invariant.
7. Test a segment-0 session search followed by a later-segment memory control sourced to the returned owner-bound message ID.
8. Run the listed tests; expected: PASS.

### Task 13: Add reaction intent to the turn plan

**Objective:** Let the existing first streamed planning record choose one supported reaction or `null`.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/tests/test_conversation_models.py`
- Modify: `tomo_core/tests/test_conversation_parsing.py`
- Modify: `tomo_core/tests/test_conversation_prompts.py`

**Steps:**
1. Define `ReactionIntent(emoji)` with a small configurable default allowlist such as `👍 ❤️ 😂 🔥 🥰 👏 🤔 👀 🙏 🫡`.
2. Accept exactly `null` or one allowlisted Unicode emoji in the turn plan; reject custom emoji IDs, arbitrary strings, and multiple reactions.
3. Prompt for `null` by default and suppress reactions for slash/auth flows, errors, routine acknowledgements, and emotionally inappropriate contexts. Use model-led sparsity; do not add a hard cooldown.
4. Consult `OwnerMemorySettings.reactions_enabled` after plan validation as a deterministic veto even if the model proposed an emoji.
5. Do not use regex or random fallback reactions. Persist only content-free reaction-delivery metadata, never reaction intent as durable personal memory.
6. Run the listed tests; expected: PASS.

### Task 14: Emit a fenced runtime reaction event

**Objective:** Surface reaction intent before frame 0 without performing connector I/O inside conversation logic.

**Files:**
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/tests/test_runtime_conversation_moves.py`

**Steps:**
1. Add `RuntimeReactionReady(emoji: str)` to `RuntimeEvent`.
2. Buffer `TurnRunStarted.plan.reaction` only until segment 0's leading memory controls have been handled; then consult the latest owner setting and emit at most one event before native tool execution or the first `RuntimeFrameReady`.
3. Check `is_active()` immediately before yielding the reaction event.
4. Keep reaction emission independent from provisional memory staging; either side effect may fail without changing the other's lifecycle.
5. Test `null`, reaction ordering, same-turn reaction opt-out, cancellation before reaction, cancellation after reaction, and no duplicate event during repair/tool segments.
6. Run: `uv run python -m unittest tests.test_runtime_conversation_moves -v`; expected: PASS.

### Task 15: Version the sandbox stream for reaction events

**Objective:** Carry reactions from Daytona to Railway without weakening the closed protocol.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py`
- Modify: `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Modify: `tomo_core/tests/test_sandbox_protocol.py`
- Modify: `tomo_core/tests/test_sandbox_inbound.py`
- Modify: `tomo_core/tests/test_sandbox_dispatch.py`

**Steps:**
1. Bump output `PROTOCOL_VERSION` to 4 and add closed-shape `SandboxReactionEvent(sequence, emoji)`.
2. Keep the host parser compatible with complete v2/v3 streams during rollout; only v4 allows reaction events.
3. Permit at most one reaction event, before any frame or terminal event, while preserving one monotonic event sequence.
4. Validate emoji against the host-owned allowlist again at the trust boundary.
5. Ensure completed-result validation compares frames independently of reaction sequence numbering.
6. Update `sandbox_inbound.run_once` to serialize `RuntimeReactionReady` and flush it immediately.
7. Run the listed tests; expected: PASS.

### Task 16: Implement Telegram `setMessageReaction`

**Objective:** Add a connector-level reaction primitive without coupling core conversation code to Telegram payloads.

**Files:**
- Modify: `tomo_core/src/tomo_core/telegram.py`
- Modify: `tomo_core/src/tomo_core/telegram_bot.py`
- Modify: `tomo_core/tests/test_telegram_bot.py`

**Steps:**
1. Extend `TelegramClient` with `set_message_reaction(actor_id, message_id, emoji)`.
2. Implement Bot API payload:

```python
self.request(
    "setMessageReaction",
    {
        "chat_id": actor_id,
        "message_id": int(message_id),
        "reaction": [{"type": "emoji", "emoji": emoji}],
        "is_big": False,
    },
)
```

3. Extend `FakeTelegramClient` to record reactions.
4. Test exact payload, invalid message ID handling, and API errors.
5. Run: `uv run python -m unittest tests.test_telegram_bot -v`; expected: PASS.

### Task 17: Reserve and deliver hosted reactions safely

**Objective:** Apply a reaction once to the latest triggering user message while respecting generation fencing.

**Files:**
- Modify: `tomo_core/src/tomo_core/onboarding_store.py`
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Modify: `tomo_core/tests/test_onboarding_store.py`
- Modify: `tomo_core/tests/test_shared_gateway.py`
- Modify: `tomo_core/tests/test_interruptible_telegram_turns.py`

**Steps:**
1. Add a versioned `telegram_reaction_deliveries` table keyed by `(generation_id, revision, target_message_id)` with emoji, status, and timestamps.
2. Add `reserve_reaction`, `mark_reaction_sent`, and `mark_reaction_failed` methods using the same transaction discipline as frame delivery.
3. On `SandboxReactionEvent`, check `is_generation_active`, reserve, check active again, then call `set_message_reaction(installation.chat_id, work.inputs[-1].message_id, emoji)`.
4. Treat Telegram rejection as best-effort: mark failed and continue consuming frames; do not fail or retry the answer generation.
5. Do not use protocol `sequence == 0` to identify the first reply frame after reactions exist. Track first delivered frame explicitly when assigning `reply_to_message_id`.
6. Test reaction before frame 0, duplicate replay, stale revision, cancellation after reserve, cancellation after API acknowledgement, unsupported Telegram reaction failure, and normal frame delivery despite reaction failure.
7. Run the listed tests; expected: PASS.

### Task 18: Support local direct delivery without duplicate reactions

**Objective:** Keep non-Daytona polling behavior equivalent while preserving one Telegram owner per path.

**Files:**
- Modify: `tomo_core/src/tomo_core/telegram.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Modify: `tomo_core/tests/test_telegram_bot.py`
- Modify: `tomo_core/tests/test_shared_gateway.py`

**Steps:**
1. Route direct local runtime reaction events through `TelegramDeliverySink.react_to_message`.
2. Keep `CollectingTelegramSink` in sandbox mode as a no-op for reactions so the sandbox cannot call Telegram.
3. Verify in-process shared dispatch returns the intent to `SharedTelegramGateway` rather than sending through the runtime’s original client.
4. Test exactly one reaction in each local/hosted path and no cross-chat target.
5. Run the listed tests; expected: PASS.

### Task 19: Add natural-language governance, owner settings, deletion, and maintenance

**Objective:** Respect reversible user governance immediately, require confirmation for permanent deletion, and preserve operability after moving personal data into SQLite.

**Files:**
- Modify: `tomo_core/src/tomo_core/personal_data.py`
- Modify: `tomo_core/src/tomo_core/sqlite_personal_data.py`
- Create: `tomo_core/src/tomo_core/memory_governance.py`
- Modify: `tomo_core/src/tomo_core/context.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/cli.py`
- Create: `tomo_core/tests/test_personal_data_maintenance.py`
- Create: `tomo_core/tests/test_memory_governance.py`

**Steps:**
1. Add failing tests for blank-query memory inspection, exact correction, reversible user disable, duplicate autonomous re-learning suppression, agent inability to reactivate user-disabled rows, and deleted facts being learnable again from future evidence.
2. Require `user_intent_excerpt` to be an exact normalized substring of the current user burst for every user-governance/settings control. Map ordinary “forget/ignore/stop using” intent to exact-ID `disable_by_user`; ambiguous matches produce no mutation and require clarification.
3. Implement `request_delete`: resolve exact owner-bound IDs, persist a short-lived `pending_memory_actions` row, hydrate only its action ID and safe target summary on the next turn, and require `confirm_delete` or cancellation. Never infer confirmation from an unrelated message.
4. Apply user disable/settings changes immediately and increment `governance_revision`, independently of conversational generation acceptance. On confirmed deletion, increment the revision, transactionally remove canonical memory content, provenance, FTS rows, and matching older provisional rows, then write a content-free idempotency tombstone. Test stale in-flight generations, expired, repeated, cross-owner, superseded, and cancelled confirmations.
5. Implement independent session deletion plus explicit `cascade_memories=True`; default deletion leaves derived memories intact with provenance marked unavailable. Test both directions and cross-owner isolation.
6. Add repository/CLI operations for owner capture/retrieval/reaction settings, `rebuild-index`, `integrity-check`, owner-scoped export, and owner deletion. Do not add dashboard UI or an unconfirmed destructive account endpoint.
7. Make `delete_owner` transactional and verify sessions, messages, memory content, provenance, FTS, pending actions, and owner settings are gone while other owners remain untouched.
8. Add provisional-artifact retention configuration; maintenance, not user turns, performs pruning. Accepted sessions and memories have no automatic expiry.
9. Run both governance and maintenance test modules; expected: PASS.

### Task 20: Update architecture and migration documentation

**Objective:** Make the portability seam and rollout procedure explicit for future adapters.

**Files:**
- Modify: `tomo_core/docs/conversation-architecture.md`
- Create: `tomo_core/docs/adr/0002-portable-personal-data-and-search.md`
- Create: `tomo_core/docs/operations/session-json-to-sqlite.md`

**Steps:**
1. Document `PersonalDataRepository` as the only runtime persistence boundary.
2. Include a backend matrix:
   - SQLite: FTS5 + `bm25`;
   - PostgreSQL/Supabase: `tsvector` + `websearch_to_tsquery`;
   - MariaDB: FULLTEXT + `MATCH AGAINST`.
3. State that repository contract tests and canonical export/import are required before switching backends.
4. Document the exact TurnRun lifecycle: persist inbound → accept prior generations → hydrate always/contextual memory → validate plan → apply leading owner-setting controls → emit permitted reaction → execute searches → permit later memory controls → emit frames → activate autonomous controls only on generation acceptance.
5. Document unrestricted semantic memory, credential exclusion, epistemic provenance, salience/surface scope, indefinite retention, independent deletion, user-disable authority, natural-language governance, and future capture/retrieval dashboard settings.
6. Document the v1 security boundary: isolated per-user volume, restrictive permissions, transport/platform encryption, no application-managed encryption, and no personal content in logs/control-plane/protocol summaries.
7. Document rollout: backup volume, deploy dual-read importer/single-write SQLite build, verify counts/integrity, retain JSON backups, then remove legacy reader in a later release.
8. Document rollback: stop runtime, restore previous build, retain untouched JSON files; never reverse-write SQLite changes into old JSON automatically.

### Task 21: Full verification and privacy review

**Objective:** Prove behavior across persistence, tool binding, protocol, cancellation, and delivery.

**Files:**
- Review all changed files
- Test: `tomo_core/tests/`

**Steps:**
1. Run focused tests for storage, importer, search tool, runtime, protocol, gateway, and Telegram API.
2. Run the complete suite:

```bash
PYTHONDONTWRITEBYTECODE=1 \
UV_PROJECT_ENVIRONMENT=/tmp/tomo-v2-memory-venv \
uv run python -m unittest discover -s tests -v
```

Expected: all tests PASS.

3. Run: `git diff --check HEAD`; expected: no output.
4. Search production logging statements, diagnostics, metrics, and protocol payloads for memory controls, statements/values, source text, message bodies, FTS queries, tool observations, OAuth tokens, and database URLs. Expected: no newly logged personal content or secrets.
5. Verify migration/export fixture counts: sessions, messages, accepted generations, unrestricted memories, provenance, settings, and tombstones match source records; FTS counts are rebuildable and need not be exported.
6. Verify local and hosted acceptance scenarios manually with fake clients:
   - arbitrary memories and qualified inferences stage immediately as provisional, then activate only after generation acceptance;
   - agent-selected `always` plus FTS-relevant `contextual` memory hydrates before segment 0 within hard prompt caps;
   - `search_memories` recovers archive/omitted memory and `search_sessions` finds cross-connector Unicode/phrase/prefix hits without crossing owners;
   - a later segment can remember an owner-bound session hit with exact provenance;
   - user disable is immediate, blocks autonomous reactivation, and remains reversible;
   - permanent deletion requires pending confirmation, removes content/FTS, and leaves only a content-free tombstone;
   - capture/retrieval/reaction owner settings are independent and authoritative;
   - reaction appears before frame 0 when selected;
   - reaction failure does not suppress frames;
   - stale/superseded generation cannot send a reaction or activate a memory.
7. Request two-stage review: spec compliance first, then code quality/security.

## Files likely to change

- `tomo_core/CONTEXT.md`
- `tomo_core/docs/conversation-architecture.md`
- `tomo_core/docs/adr/0002-portable-personal-data-and-search.md`
- `tomo_core/docs/operations/session-json-to-sqlite.md`
- `tomo_core/src/tomo_core/personal_data.py`
- `tomo_core/src/tomo_core/sqlite_personal_data.py`
- `tomo_core/src/tomo_core/personal_data_transfer.py`
- `tomo_core/src/tomo_core/legacy_session_import.py`
- `tomo_core/src/tomo_core/personal_search_tools.py`
- `tomo_core/src/tomo_core/memory_governance.py`
- `tomo_core/src/tomo_core/sessions.py`
- `tomo_core/src/tomo_core/context.py`
- `tomo_core/src/tomo_core/models.py`
- `tomo_core/src/tomo_core/runtime.py`
- `tomo_core/src/tomo_core/instances.py`
- `tomo_core/src/tomo_core/tools.py`
- `tomo_core/src/tomo_core/conversation/models.py`
- `tomo_core/src/tomo_core/conversation/parsing.py`
- `tomo_core/src/tomo_core/conversation/prompts.py`
- `tomo_core/src/tomo_core/conversation/framing.py`
- `tomo_core/src/tomo_core/sandbox_protocol.py`
- `tomo_core/src/tomo_core/sandbox_inbound.py`
- `tomo_core/src/tomo_core/sandbox_dispatch.py`
- `tomo_core/src/tomo_core/telegram.py`
- `tomo_core/src/tomo_core/telegram_bot.py`
- `tomo_core/src/tomo_core/onboarding_store.py`
- `tomo_core/src/tomo_core/shared_gateway.py`
- corresponding `tomo_core/tests/test_*.py` files listed per task

## Risks and tradeoffs

- **Autonomous memory quality:** Tomo may retain uncertain or assistant-derived conclusions. Provenance, confidence, temporal validity, and epistemic labels are mandatory so retrieval does not silently upgrade them to user-stated truth.
- **Memory contradiction:** subject/topic/value normalization is imperfect. Exact corrections may supersede while unresolved conflicts coexist with qualifiers. Semantic entity resolution and model-based consolidation are deliberately deferred.
- **Context pressure:** unrestricted storage cannot imply unrestricted prompt injection. Configurable always/contextual record and character caps are runtime safety limits; omitted memory remains searchable.
- **Governance races:** user disable/delete bypasses generation acceptance while autonomous writes remain fenced. Exact stable IDs, pending confirmation rows, transactions, and idempotency tombstones prevent stale model output from overriding user intent.
- **SQLite concurrency:** one sandbox per user makes contention modest, but overlapping commands can occur. WAL, short transactions, busy timeout, and contract tests are required.
- **FTS portability:** query syntax/ranking differ across vendors. The adapter returns normalized domain hits; exact scores are not portable and tests should assert ordering/visibility, not identical numeric values.
- **Protocol rollout:** v4 writers cannot be deployed before Railway readers accept v4. Deploy tolerant readers first, then sandbox writers/snapshot.
- **Reaction races:** Telegram side effects cannot be recalled after acknowledgement. The gateway checks the generation fence immediately before sending; that is the strongest practical guarantee.
- **Legacy rollback:** once new SQLite-only turns exist, an old JSON build cannot see them. Keep the rollout window short and use canonical export rather than attempting unsafe reverse synchronization.
- **Sensitive personal data:** V1 semantically permits sensitive personal memory except credentials/authentication secrets. The database and FTS are not application-key encrypted; deployment relies on isolated volumes, restrictive permissions, encrypted transport, and platform encryption at rest without overstating guarantees.

## Deferred follow-ups

- Dashboard UI/API for per-memory inspect, pin/scope override, disable/re-enable, correction, deletion, explicit session-to-memory cascade, and the capture/retrieval master switch. Repository semantics and natural-language governance ship first.
- Application-managed encryption/SQLCipher or a separate encrypted non-FTS credential vault.
- Semantic/vector retrieval and embeddings.
- Cross-user/shared memories.
- A PostgreSQL/Supabase or MariaDB production adapter; this plan creates and tests the seam but does not add an unused second production dependency.
- Reaction analytics or aggressive reaction frequency heuristics.
- Deleting retained legacy JSON backups after a verified retention window.

## Optional commit checkpoints

Only if the user explicitly asks for commits:

```bash
# optional
# 1. feat(storage): add portable personal data repository and sqlite adapter
# 2. feat(memory): add fenced autonomous memory controls and governance
# 3. feat(search): add bound fts5 memory and session search tools
# 4. feat(telegram): add fenced message reactions and protocol v4
# 5. docs: document personal data migration and backend portability
```

Before implementation, re-run `git status --short` and preserve all unrelated TurnRun working-tree changes.