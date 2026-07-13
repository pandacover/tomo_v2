# Daytona SQLite Checkpoints

Daytona-mounted volumes must not host an active SQLite database. SQLite journal
and VFS choices fail at commit on these mounts. `TOMO_CORE_DATA_DIR=/var/lib/tomo`
therefore remains a durable checkpoint prefix, while sandbox runtime explicitly
sets its local SQLite work directory to `/tmp/tomo-core-sqlite`.

The repository stores two alternating opaque checkpoint slots beside that
prefix. Each slot contains a versioned header with a magic value, generation,
payload length, and SHA-256, followed by a closed SQLite backup. On startup the
local work copy is restored from the highest generation with a valid frame and
SQLite integrity check; a damaged newest slot falls back to the older slot.

After each successful mutation, the repository takes `Connection.backup` into a
temporary local file, closes and verifies it, then writes and fsyncs the next
durable slot directly. It never renames, replaces, or opens SQLite files on the
durable volume, and no `-wal` or `-shm` files belong there. A local inter-process
lock serializes restore and checkpoint generation so checkpoints cannot regress.
