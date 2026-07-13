# ADR 0002: Portable Personal Data and Search

## Status

Accepted

## Decision

`PersonalDataRepository` is the only runtime persistence boundary for
owner-scoped sessions, memories, governance settings, and search. Adapters own
connections, transactions, SQL, and search syntax. Runtime code does not use
SQLite objects or FTS expressions.

SQLite is the v1 adapter at `<data-dir>/tomo.sqlite3`. It requires FTS5 and
uses FTS5 with `bm25`; FTS tables are disposable indexes. A future adapter must
pass the repository contract tests and round-trip canonical versioned JSONL
before configuration switches to it.

Hosted Daytona sandboxes use the same durable data directory only as a
two-slot framed checkpoint prefix. Their active SQLite database is a local work
copy, never a file on the mounted volume. Each committed repository mutation
backs up the closed local database and fsyncs it into the alternating durable
slot. Direct SQLite access to Daytona-mounted paths is forbidden because their
filesystem cannot reliably commit SQLite writes.

| Backend | Search implementation |
|---|---|
| SQLite | FTS5 and `bm25` |
| PostgreSQL/Supabase | `tsvector` and `websearch_to_tsquery` |
| MariaDB | FULLTEXT and `MATCH AGAINST` |

The canonical JSONL stream is backend-neutral and owner-scoped. It carries
canonical sessions, messages, accepted generations, memories, provenance,
owner settings, eligible pending actions, and content-free deletion tombstones.
It excludes FTS rows.

## Consequences

Memory has no semantic whitelist except credentials and authentication secrets.
Each record retains epistemic provenance, confidence, salience, temporal data,
and an `always`, `contextual`, or `archive` surface scope. Accepted sessions and
memories are retained indefinitely. User disable is immediate and authoritative;
permanent deletion requires a pending confirmation. Session deletion does not
delete memory unless an explicit cascade is requested.

V1 does not provide application-managed or field encryption. Operators rely on
per-user sandbox or volume isolation, restrictive permissions, encrypted
transport, platform or volume encryption at rest, and backups. Personal content
must not enter logs, control-plane diagnostics, or sandbox completion summaries.

Dashboard controls for capture, retrieval, and reactions, and
application-managed encryption, remain deferred.
