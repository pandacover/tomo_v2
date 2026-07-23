# Session JSON Retirement Operations

## Data location and identity

All commands use `--data-dir`; otherwise the CLI reads `TOMO_CORE_DATA_DIR`,
defaulting to `.tomo_core` relative to the current working directory. The
database is `<data-dir>/tomo.sqlite3`. `TOMO_DATA_DIR` is not read by this
implementation.

Local direct runtime defaults to owner `local`. Hosted sandbox runtime requires
`TOMO_INSTANCE_ID`; missing identity produces `missing_owner_id`. Always use
the Tomo owner ID with `--owner`, never a connector actor ID.

## Completed cutover and cleanup

SQLite is authoritative. The runtime no longer reads or imports
`<data-dir>/sessions/*.json`, including recovery copies.

After a volume-level backup and inspection of any legacy files, operators may
delete only `<data-dir>/sessions`. Do not delete `tomo.sqlite3`, checkpoint
files, or any current SQLite storage. Legacy JSON cannot restore deleted SQLite
owner data.

## Supported CLI commands

Run these from `tomo_core` with `PYTHONPATH=src`, or use the installed
`tomo-core` executable in place of `python -m tomo_core.cli`. Do not treat a
direct CLI invocation against a mounted production volume as safe; use the
deployment's approved maintenance workflow.

```bash
PYTHONPATH=src python -m tomo_core.cli personal-data integrity-check --data-dir /secure/tomo
PYTHONPATH=src python -m tomo_core.cli personal-data rebuild-index --data-dir /secure/tomo --owner tomo-123
PYTHONPATH=src python -m tomo_core.cli personal-data export --data-dir /secure/tomo --owner tomo-123 --output /secure/backups/tomo-123.jsonl
PYTHONPATH=src python -m tomo_core.cli personal-data purge-window --data-dir /secure/tomo --owner tomo-123 --start 2026-07-20T18:30:00Z --end 2026-07-23T06:00:00Z --backup /secure/backups/tomo-123-before-purge.jsonl --confirm
PYTHONPATH=src python -m tomo_core.cli personal-data settings --data-dir /secure/tomo --owner tomo-123
PYTHONPATH=src python -m tomo_core.cli personal-data settings --data-dir /secure/tomo --owner tomo-123 --capture-enabled false
PYTHONPATH=src python -m tomo_core.cli personal-data settings --data-dir /secure/tomo --owner tomo-123 --retrieval-enabled false
PYTHONPATH=src python -m tomo_core.cli personal-data settings --data-dir /secure/tomo --owner tomo-123 --reactions-enabled false
PYTHONPATH=src python -m tomo_core.cli personal-data delete-owner --data-dir /secure/tomo --owner tomo-123 --confirm
```

`integrity-check` returns `ok` on success and a nonzero status on failure.
`rebuild-index` is safe because it rebuilds disposable FTS rows from canonical
tables. Export writes canonical format version 1 JSONL and scopes every record
to the requested owner. Treat `delete-owner --confirm` as irreversible: it
removes that owner's sessions, messages, memories, provenance, settings,
pending actions, tombstones, and search rows while leaving other owners alone.
`purge-window` requires an exclusive canonical backup path and `--confirm`. It
deletes only that owner's messages in the half-open `[start, end)` window,
associated accepted generations, derived memory revisions and pending memory
actions. When a deleted memory revision superseded an older retained record,
the older record is restored. The command rebuilds owner search indexes and
checks database integrity before reporting counts; peer relationship history,
onboarding identity, credentials, and other owners are untouched.

There is no CLI command for canonical import, session deletion, or provisional
artifact pruning. The repository provides `import_owner`, `delete_session`,
and `prune_provisional_artifacts` for controlled application operations;
maintenance pruning removes only provisional memories older than its supplied
cutoff plus expired pending deletion actions. It never expires accepted
sessions or accepted memories.

## Governance and reactions

Capture, retrieval, and reactions default to enabled and are independent.
Natural-language controls can inspect or correct retained memory, disable an
exact owner-bound record, request permanent deletion, and confirm or cancel the
pending delete. A requested permanent deletion is pending for ten minutes and
requires its action ID plus an exact current-burst intent excerpt. Deleting a
session does not delete derived memory unless application code explicitly uses
`cascade_memories=True`.

Reactions are model-led and normally `null`. One allowlisted emoji may be sent
for the latest message in a burst after leading setting controls validate and
before tools or frames. Owner opt-out vetoes it. The v3 sandbox event is bound
to owner, actor, chat, generation, revision, and target message; gateway
deduplication and active-generation checks prevent stale or duplicate delivery.
Telegram reaction failure is isolated from the answer.

## Security boundary

SQLite is not application-encrypted in v1. Keep the data directory on an
isolated per-user sandbox or volume with least-privilege file access, encrypted
backups and storage where available, and encrypted transport. FTS data is
searchable local content and needs the same protection. Do not place database
copies, JSONL exports, memory statements, credentials, or authentication
secrets in logs or control-plane diagnostics.
