# Session JSON to SQLite Operations

## Data location and identity

All commands use `--data-dir`; otherwise the CLI reads `TOMO_CORE_DATA_DIR`,
defaulting to `.tomo_core` relative to the current working directory. The
database is `<data-dir>/tomo.sqlite3`. `TOMO_DATA_DIR` is not read by this
implementation.

Local direct runtime defaults to owner `local`. Hosted sandbox runtime requires
`TOMO_INSTANCE_ID`; missing identity produces `missing_owner_id`. Always use
the Tomo owner ID with `--owner`, never a connector actor ID.

## Rollout and rollback

1. Stop the runtime and take a volume-level backup of the data directory.
2. Deploy the SQLite build. Runtime initialization imports
   `<data-dir>/sessions/*.json` once for its owner, including a `.json.recovery`
   file only when its primary JSON is missing or invalid.
3. Verify integrity and export each owner before declaring the rollout complete.
4. Retain the untouched JSON files and volume backup. New runtime writes go to
   SQLite only.

The importer records each source path and SHA-256 with its imported rows. A
matching file is skipped on later starts. A changed already-imported file fails
with `legacy_session_file_changed`; invalid data without a usable recovery copy
fails with `invalid_legacy_session`. Do not edit, delete, or reverse-write the
legacy JSON files during recovery.

To roll back, stop the SQLite build and restore the prior build against the
untouched JSON files. Do not automatically copy SQLite changes back into JSON.
To restore portable data into an adapter, use the canonical JSONL import API in
the target deployment, rebuild its index, verify counts and integrity, then
switch configuration. There is currently no `personal-data import` CLI command.

## Supported CLI commands

Run these from `tomo_core` with `PYTHONPATH=src`, or use the installed
`tomo-core` executable in place of `python -m tomo_core.cli`.

```bash
PYTHONPATH=src python -m tomo_core.cli personal-data integrity-check --data-dir /secure/tomo
PYTHONPATH=src python -m tomo_core.cli personal-data rebuild-index --data-dir /secure/tomo --owner tomo-123
PYTHONPATH=src python -m tomo_core.cli personal-data export --data-dir /secure/tomo --owner tomo-123 --output /secure/backups/tomo-123.jsonl
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
