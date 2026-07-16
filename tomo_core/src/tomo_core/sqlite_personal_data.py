from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import time
import uuid
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path

from .models import utc_now_iso
from .personal_data import (MemoryContextQuery, MemoryGovernanceControl, MemoryGovernanceResult, MemoryRecord,
                            MemorySearchHit, MemorySearchQuery, MemorySourceRef, MemoryWriteControl,
                             OwnerMemorySettings, OwnerSettingControl, PendingMemoryAction, PendingMemoryActionControl, SessionSearchHit,
                            SessionSearchQuery, StorageBusyError, StorageCapabilityError, StorageSearchError)
from .sessions import ConversationSession, StoredMessage

_NS = uuid.UUID("c4bb268a-a6b1-4ee9-b3bf-9ef69f2696bc")
_CHECKPOINT_MAGIC = b"TOMOCP01"
_CHECKPOINT_VERSION = 1
_CHECKPOINT_HEADER = struct.Struct("!8sBQQ32s")


def _id(*parts: object) -> str:
    return str(uuid.uuid5(_NS, "\x1f".join(map(str, parts))))


def _fts(text: str) -> str:
    # Only words and quoted phrases become FTS tokens; model syntax never reaches MATCH.
    phrases = re.findall(r'"([^"\n]{1,128})"', text)
    words = re.findall(r"[\w]+", re.sub(r'"[^"\n]*"', "", text), flags=re.UNICODE)
    terms = ['"' + p.replace('"', '') + '"' for p in phrases] + ['"' + w + '"*' for w in words[:32]]
    if not terms:
        raise ValueError("search text must contain searchable terms")
    return " AND ".join(terms)


def _validate_timestamp(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("timestamp must be timezone-aware ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp must be timezone-aware ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware ISO-8601")


def _validate_control_timestamps(control: MemoryWriteControl) -> None:
    _validate_timestamp(control.valid_from)
    _validate_timestamp(control.valid_until)
    for source in control.sources:
        _validate_timestamp(source.observed_at)


_SCHEMA = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE sessions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,session_key TEXT NOT NULL,connector TEXT NOT NULL,actor_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,current_generation_id TEXT,current_revision INTEGER,UNIQUE(owner_id,session_key));
CREATE TABLE messages(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,role TEXT NOT NULL CHECK(role IN ('user','assistant')),content TEXT NOT NULL,timestamp TEXT NOT NULL,ordinal INTEGER,connector_message_id TEXT,update_id INTEGER,burst_id TEXT,generation_id TEXT,generation_status TEXT CHECK(generation_status IS NULL OR generation_status IN ('provisional','accepted')),metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE UNIQUE INDEX messages_user_delivery_identity ON messages(session_id,burst_id,update_id) WHERE role='user' AND burst_id IS NOT NULL AND update_id IS NOT NULL;
CREATE UNIQUE INDEX messages_assistant_generation_identity ON messages(session_id,generation_id) WHERE role='assistant' AND generation_id IS NOT NULL;
CREATE TABLE accepted_generations(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,generation_id TEXT NOT NULL,accepted_at TEXT NOT NULL,PRIMARY KEY(session_id,generation_id));
CREATE TABLE memories(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,kind TEXT NOT NULL,subject_key TEXT NOT NULL,topic TEXT NOT NULL,value_json TEXT NOT NULL,statement TEXT NOT NULL,search_text TEXT NOT NULL,fingerprint TEXT NOT NULL,epistemic_kind TEXT NOT NULL,status TEXT NOT NULL,confidence REAL NOT NULL,salience REAL NOT NULL,surface_scope TEXT NOT NULL,source_generation_id TEXT,source_session_key TEXT,source_governance_revision INTEGER NOT NULL,control_action TEXT NOT NULL DEFAULT 'upsert',supersedes_id TEXT REFERENCES memories(id) ON DELETE SET NULL,valid_from TEXT,valid_until TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX memories_owner_status_scope_salience ON memories(owner_id,status,surface_scope,salience,updated_at); CREATE INDEX memories_owner_fingerprint ON memories(owner_id,fingerprint,status);
CREATE TABLE memory_sources(memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,observed_at TEXT NOT NULL,source_available INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(memory_id,source_kind,source_id));
CREATE TABLE owner_memory_settings(owner_id TEXT PRIMARY KEY,capture_enabled INTEGER NOT NULL DEFAULT 1,retrieval_enabled INTEGER NOT NULL DEFAULT 1,reactions_enabled INTEGER NOT NULL DEFAULT 1,governance_revision INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
CREATE TABLE pending_memory_actions(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,session_key TEXT NOT NULL,action TEXT NOT NULL,target_ids_json TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE memory_deletion_tombstones(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,deleted_at TEXT NOT NULL,request_key TEXT NOT NULL,UNIQUE(owner_id,request_key));
CREATE VIRTUAL TABLE messages_fts USING fts5(record_id UNINDEXED,owner_id UNINDEXED,content,tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE memories_fts USING fts5(record_id UNINDEXED,owner_id UNINDEXED,search_text,tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN INSERT INTO messages_fts(record_id,owner_id,content) SELECT new.id,s.owner_id,new.content FROM sessions s WHERE s.id=new.session_id; END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN DELETE FROM messages_fts WHERE record_id=old.id; END;
CREATE TRIGGER messages_au AFTER UPDATE OF content ON messages BEGIN DELETE FROM messages_fts WHERE record_id=old.id; INSERT INTO messages_fts(record_id,owner_id,content) SELECT new.id,s.owner_id,new.content FROM sessions s WHERE s.id=new.session_id; END;
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN INSERT INTO memories_fts VALUES(new.id,new.owner_id,new.search_text); END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN DELETE FROM memories_fts WHERE record_id=old.id; END;
CREATE TRIGGER memories_au AFTER UPDATE OF search_text ON memories BEGIN DELETE FROM memories_fts WHERE record_id=old.id; INSERT INTO memories_fts VALUES(new.id,new.owner_id,new.search_text); END;
"""


def _schema_statements() -> tuple[str, ...]:
    statements: list[str] = []
    current = ""
    for character in _SCHEMA:
        current += character
        if sqlite3.complete_statement(current):
            statements.append(current)
            current = ""
    if current.strip():
        raise StorageCapabilityError("invalid_schema_migration")
    return tuple(statements)


def _checkpoint_after_write(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        if result is not False:
            self._checkpoint()
        return result
    return wrapped


class SqlitePersonalDataRepository:
    def __init__(self, path: str | Path, *, local_work_dir: str | Path | None = None) -> None:
        self.path = str(path)
        self._durable_path = Path(path)
        self._local_work_dir = Path(local_work_dir) if local_work_dir is not None else None
        if self._local_work_dir is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        else:
            self._local_work_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self._local_work_dir, 0o700)
            except OSError:
                pass
            name = hashlib.sha256(str(self._durable_path).encode("utf-8")).hexdigest()[:24]
            self.path = str(self._local_work_dir / f"tomo-{name}.sqlite3")
            self._lock_path = self._local_work_dir / f"tomo-{name}.lock"
            with self._checkpoint_lock():
                if not Path(self.path).is_file():
                    payload = self._newest_checkpoint()
                    if payload is not None:
                        self._restore_local(payload)
        with self._connection() as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                version = con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] if self._exists(con, "schema_migrations") else None
                if version not in (None, 1, 2, 3): raise StorageCapabilityError("unsupported_schema_version")
                if version is None:
                    for statement in _schema_statements():
                        con.execute(statement)
                    con.execute("INSERT INTO schema_migrations VALUES(1,?)", (utc_now_iso(),))
                    version = 1
                if version == 1:
                    columns = {row["name"] for row in con.execute("PRAGMA table_info(memories)")}
                    if "source_session_key" not in columns:
                        con.execute("ALTER TABLE memories ADD COLUMN source_session_key TEXT")
                    if "control_action" not in columns:
                        con.execute("ALTER TABLE memories ADD COLUMN control_action TEXT NOT NULL DEFAULT 'upsert'")
                    con.execute("INSERT INTO schema_migrations VALUES(2,?)", (utc_now_iso(),))
                    version = 2
                if version == 2:
                    columns = {row["name"] for row in con.execute("PRAGMA table_info(sessions)")}
                    if "current_generation_id" not in columns:
                        con.execute("ALTER TABLE sessions ADD COLUMN current_generation_id TEXT")
                    if "current_revision" not in columns:
                        con.execute("ALTER TABLE sessions ADD COLUMN current_revision INTEGER")
                    con.execute("INSERT INTO schema_migrations VALUES(3,?)", (utc_now_iso(),))
                con.commit()
            except sqlite3.OperationalError as error:
                con.rollback()
                if "fts5" in str(error).lower(): raise StorageCapabilityError("sqlite_fts5_unavailable") from None
                raise self._safe(error)

    def _checkpoint_slot(self, index):
        return self._durable_path.with_name(f"{self._durable_path.name}.checkpoint.{index}")

    def _checkpoint_payload(self, path):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise StorageBusyError("storage_operation_failed") from None
        if len(raw) < _CHECKPOINT_HEADER.size:
            return None
        magic, version, generation, length, digest = _CHECKPOINT_HEADER.unpack(raw[:_CHECKPOINT_HEADER.size])
        payload = raw[_CHECKPOINT_HEADER.size:]
        if magic != _CHECKPOINT_MAGIC or version != _CHECKPOINT_VERSION or length != len(payload):
            return None
        if hashlib.sha256(payload).digest() != digest:
            return None
        return generation, payload

    def _newest_checkpoint(self):
        checkpoints = []
        any_slot_exists = False
        for index in (0, 1):
            slot = self._checkpoint_slot(index)
            try:
                slot.stat()
            except FileNotFoundError:
                continue
            except OSError:
                raise StorageBusyError("storage_operation_failed") from None
            any_slot_exists = True
            checkpoint = self._checkpoint_payload(slot)
            if checkpoint is not None:
                checkpoints.append(checkpoint)
        valid = sorted(checkpoints, reverse=True)
        for _, payload in valid:
            try:
                integral = self._snapshot_is_integral(payload)
            except StorageBusyError:
                raise
            except OSError:
                raise StorageBusyError("storage_operation_failed") from None
            if integral:
                return payload
        if any_slot_exists:
            raise StorageBusyError("storage_operation_failed")
        return None

    def _snapshot_is_integral(self, payload):
        descriptor, temporary = tempfile.mkstemp(prefix=".validate-", dir=self._local_work_dir)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(payload)
            con = sqlite3.connect(temporary)
            try:
                return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            finally:
                con.close()
        except sqlite3.Error:
            return False
        finally:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                raise StorageBusyError("storage_operation_failed") from None

    def _restore_local(self, payload):
        descriptor, temporary = tempfile.mkstemp(prefix=".restore-", dir=self._local_work_dir)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @contextmanager
    def _checkpoint_lock(self):
        if self._local_work_dir is None:
            yield
            return
        lock = None
        try:
            try:
                lock = self._lock_path.open("r+b")
            except FileNotFoundError:
                try:
                    lock = self._lock_path.open("x+b")
                except FileExistsError:
                    lock = self._lock_path.open("r+b")
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 5
                while True:
                    try:
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise StorageBusyError("storage_busy") from None
                        time.sleep(0.05)
            else:
                import fcntl

                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise StorageBusyError("storage_busy") from None
                        time.sleep(0.05)
        except StorageBusyError:
            if lock is not None:
                lock.close()
            raise
        except OSError:
            if lock is not None:
                lock.close()
            raise StorageBusyError("storage_busy") from None
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                raise StorageBusyError("storage_busy") from None
            finally:
                try:
                    lock.close()
                except OSError:
                    raise StorageBusyError("storage_busy") from None

    def _fsync_checkpoint_directory(self, directory):
        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
                raise

    def _checkpoint(self, *, scrub=False):
        if self._local_work_dir is None:
            return
        with self._checkpoint_lock():
            temporary = None
            try:
                valid = [(index, self._checkpoint_payload(self._checkpoint_slot(index))) for index in (0, 1)]
                valid = [(index, checkpoint) for index, checkpoint in valid if checkpoint is not None]
                generation = max((checkpoint[0] for _, checkpoint in valid), default=0)
                slots = (0, 1) if scrub else (1 - max(valid, key=lambda item: item[1][0])[0] if valid else 0,)
                descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=self._local_work_dir)
                os.chmod(temporary, 0o600)
                os.close(descriptor)
                with ExitStack() as connections:
                    target = connections.enter_context(closing(sqlite3.connect(temporary)))
                    source = connections.enter_context(closing(sqlite3.connect(self.path)))
                    source.backup(target)
                with closing(sqlite3.connect(temporary)) as check:
                    if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise StorageBusyError("storage_operation_failed")
                payload = Path(temporary).read_bytes()
                digest = hashlib.sha256(payload).digest()
                for index in slots:
                    generation += 1
                    framed = _CHECKPOINT_HEADER.pack(_CHECKPOINT_MAGIC, _CHECKPOINT_VERSION, generation, len(payload), digest) + payload
                    slot = self._checkpoint_slot(index)
                    new_slot = not slot.exists()
                    slot.parent.mkdir(parents=True, exist_ok=True)
                    with slot.open("wb") as file:
                        try:
                            os.chmod(slot, 0o600)
                        except OSError:
                            pass
                        file.write(framed)
                        file.flush()
                        os.fsync(file.fileno())
                    if new_slot:
                        self._fsync_checkpoint_directory(slot.parent)
            except sqlite3.Error as error:
                raise self._safe(error) from None
            except StorageBusyError:
                raise
            except OSError:
                raise StorageBusyError("storage_operation_failed") from None
            finally:
                if temporary is not None:
                    try:
                        Path(temporary).unlink(missing_ok=True)
                    except OSError:
                        pass

    @staticmethod
    def _exists(con, name): return con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None
    @contextmanager
    def _connection(self):
        con = None
        try:
            con = sqlite3.connect(self.path, timeout=5, isolation_level=None); con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON"); con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA synchronous=FULL"); con.execute("PRAGMA busy_timeout=5000"); yield con
        except sqlite3.OperationalError as error:
            raise self._safe(error) from None
        finally:
            if con is not None: con.close()
    @staticmethod
    def _safe(error):
        if "locked" in str(error).lower() or "busy" in str(error).lower(): return StorageBusyError("storage_busy")
        return StorageBusyError("storage_operation_failed")
    def _session(self, con, owner, key, create=False):
        row=con.execute("SELECT * FROM sessions WHERE owner_id=? AND session_key=?",(owner,key)).fetchone()
        if row or not create:return row
        now=utc_now_iso(); bits=key.split(":",2); connector=bits[0] if bits else "unknown"; actor=bits[2] if len(bits) > 2 and bits[1] == "actor" else bits[1] if len(bits) > 1 else "unknown"; sid=_id("session",owner,key)
        con.execute("INSERT INTO sessions(id,owner_id,session_key,connector,actor_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(sid,owner,key,connector,actor,now,now)); return con.execute("SELECT * FROM sessions WHERE id=?",(sid,)).fetchone()

    def load_session(self, owner_id, session_key):
        with self._connection() as con:
            s=self._session(con,owner_id,session_key); result=ConversationSession(session_key)
            if not s:return result
            accepted=con.execute("SELECT generation_id FROM accepted_generations WHERE session_id=?",(s["id"],)).fetchall(); result.accepted_generation_ids=tuple(sorted(r[0] for r in accepted))
            for r in con.execute("SELECT * FROM messages WHERE session_id=? ORDER BY COALESCE(ordinal,2147483647),created_at,id",(s["id"],)):
                meta=json.loads(r["metadata_json"]); meta.update({k:r[k] for k in ("ordinal","connector_message_id","update_id","burst_id","generation_id","generation_status") if r[k] is not None}); meta["_canonical_message_id"] = r["id"]
                if r["connector_message_id"] is not None: meta["message_id"]=r["connector_message_id"]
                result.append(StoredMessage(r["role"],r["content"],r["timestamp"],meta))
            return result
    def current_session_revision(self, owner_id, session_key):
        with self._connection() as con:
            row = con.execute(
                "SELECT current_revision FROM sessions WHERE owner_id=? AND session_key=?",
                (owner_id, session_key),
            ).fetchone()
            return None if row is None or row["current_revision"] is None else int(row["current_revision"])
    @_checkpoint_after_write
    def save_session(self, owner_id, session, *, generation_id=None, revision=None):
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE"); s=self._session(con,owner_id,session.session_key,True); now=utc_now_iso()
                if (generation_id is None) != (revision is None):
                    raise ValueError("generation_id and revision must be provided together")
                if generation_id is not None:
                    current_revision = s["current_revision"]
                    current_generation = s["current_generation_id"]
                    if current_revision is not None and (revision < current_revision or (revision == current_revision and generation_id != current_generation)):
                        con.rollback(); return False
                    if current_revision is None or revision > current_revision:
                        con.execute("UPDATE sessions SET current_generation_id=?,current_revision=? WHERE id=?",(generation_id,revision,s["id"]))
                for pos,m in enumerate(session.messages):
                    d=m.metadata; role=m.role; gen=d.get("generation_id"); burst=d.get("burst_id"); update=d.get("update_id")
                    identity_metadata = {key: value for key, value in d.items() if key not in {"ordinal", "connector_message_id", "update_id", "burst_id", "generation_id", "generation_status", "message_id"}}
                    mid=d.get("legacy_message_id") or (_id("message",s["id"],role, burst,update) if role=="user" and update is not None else _id("message",s["id"],role,gen) if role=="assistant" and gen else _id("message",s["id"],role,m.content,m.timestamp,json.dumps(identity_metadata,sort_keys=True,default=str)))
                    status="accepted" if role=="assistant" and gen in session.accepted_generation_ids else d.get("generation_status")
                    if role == "assistant" and gen:
                        con.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,timestamp=excluded.timestamp,ordinal=excluded.ordinal,generation_status=excluded.generation_status,metadata_json=excluded.metadata_json",(mid,s["id"],role,m.content,m.timestamp,d.get("ordinal",pos),d.get("message_id"),update,burst,gen,status,json.dumps(d,sort_keys=True,default=str),now))
                    else:
                        con.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(mid,s["id"],role,m.content,m.timestamp,d.get("ordinal",pos),d.get("message_id"),update,burst,gen,status,json.dumps(d,sort_keys=True,default=str),now))
                existing_generations = {
                    row[0] for row in con.execute(
                        "SELECT generation_id FROM accepted_generations WHERE session_id=?", (s["id"],)
                    )
                }
                requested_generations = tuple(
                    gen for gen in session.accepted_generation_ids if gen not in existing_generations
                )
                for gen in requested_generations:
                    # Acceptance is meaningful only for an assistant generation persisted in
                    # this owner session; arbitrary caller IDs cannot unlock memory controls.
                    con.execute(
                        "INSERT OR IGNORE INTO accepted_generations "
                        "SELECT ?, ?, ? WHERE EXISTS ("
                        "SELECT 1 FROM messages WHERE session_id=? AND role='assistant' AND generation_id=?)",
                        (s["id"], gen, now, s["id"], gen),
                    )
                newly_accepted = tuple(
                    row[0] for row in con.execute(
                        "SELECT generation_id FROM accepted_generations WHERE session_id=?", (s["id"],)
                    ) if row[0] not in existing_generations
                )
                con.execute("UPDATE messages SET generation_status='accepted' WHERE session_id=? AND generation_id IN (SELECT generation_id FROM accepted_generations WHERE session_id=?)",(s["id"],s["id"]))
                # A provisional control can become visible only in its original session and
                # only while the owner's governance revision still matches its turn start.
                provisional = ()
                if newly_accepted:
                    placeholders = ",".join("?" for _ in newly_accepted)
                    provisional = con.execute(
                        "SELECT * FROM memories WHERE owner_id=? AND status='provisional' "
                        "AND source_session_key=? AND source_generation_id IN ("
                        f"{placeholders})",
                        (owner_id, session.session_key, *newly_accepted),
                    ).fetchall()
                setting = con.execute("SELECT governance_revision FROM owner_memory_settings WHERE owner_id=?", (owner_id,)).fetchone()
                revision = setting[0] if setting else 0
                for incoming in provisional:
                    if incoming["source_governance_revision"] != revision:
                        con.execute("UPDATE memories SET status='superseded',updated_at=? WHERE id=?", (now, incoming["id"]))
                        continue
                    target = incoming["supersedes_id"]
                    action = incoming["control_action"]
                    if action == "remove":
                        if target:
                            con.execute("UPDATE memories SET status='superseded',updated_at=? WHERE owner_id=? AND id=?", (now, owner_id, target))
                        con.execute("UPDATE memories SET status='superseded',updated_at=? WHERE id=?", (now, incoming["id"]))
                    elif action in {"archive", "disable_by_agent"}:
                        if target:
                            status = "archived" if action == "archive" else "disabled_by_agent"
                            con.execute("UPDATE memories SET status=?,updated_at=? WHERE owner_id=? AND id=? AND status!='disabled_by_user'", (status, now, owner_id, target))
                        con.execute("UPDATE memories SET status='superseded',updated_at=? WHERE id=?", (now, incoming["id"]))
                    else:
                        if action == "upsert" and target:
                            con.execute("UPDATE memories SET status='superseded',updated_at=? WHERE owner_id=? AND id=? AND status!='disabled_by_user'", (now, owner_id, target))
                        status = "archived" if incoming["surface_scope"] == "archive" else "active"
                        con.execute("UPDATE memories SET status=?,updated_at=? WHERE id=?", (status, now, incoming["id"]))
                con.execute("UPDATE sessions SET updated_at=? WHERE id=?",(now,s["id"])); con.commit(); return True
        except sqlite3.OperationalError as e: raise self._safe(e) from None
    def accept_generations(self, owner_id, session_key, generation_ids):
        session=self.load_session(owner_id,session_key); session.accept_generations(generation_ids); self.save_session(owner_id,session)

    def memory_settings(self, owner):
        with self._connection() as c:
            r=c.execute("SELECT * FROM owner_memory_settings WHERE owner_id=?",(owner,)).fetchone()
            return OwnerMemorySettings(owner, True, True, True, 0) if r is None else OwnerMemorySettings(owner,bool(r["capture_enabled"]),bool(r["retrieval_enabled"]),bool(r["reactions_enabled"]),r["governance_revision"])
    @_checkpoint_after_write
    def update_memory_setting(self, owner, setting, enabled):
        if setting not in {"capture_enabled","retrieval_enabled","reactions_enabled"}: raise ValueError("unknown owner setting")
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); now=utc_now_iso(); c.execute("INSERT OR IGNORE INTO owner_memory_settings VALUES(?,?,?,?,?,?)",(owner,1,1,1,0,now)); c.execute(f"UPDATE owner_memory_settings SET {setting}=?,governance_revision=governance_revision+1,updated_at=? WHERE owner_id=?",(int(enabled),now,owner)); c.commit()
        return self.memory_settings(owner)
    def _record(self,c,r):
        src=tuple(MemorySourceRef(x["source_kind"],x["source_id"],x["observed_at"],bool(x["source_available"])) for x in c.execute("SELECT * FROM memory_sources WHERE memory_id=?",(r["id"],)))
        return MemoryRecord(r["id"],r["owner_id"],r["kind"],r["subject_key"],r["topic"],json.loads(r["value_json"]),r["statement"],r["epistemic_kind"],r["status"],r["confidence"],r["salience"],r["surface_scope"],r["valid_from"],r["valid_until"],src)
    @_checkpoint_after_write
    def stage_memory_controls(self, owner, session_key, generation_id, segment_index, governance_revision, controls, *, revision=None):
        try:
            with self._connection() as c:
                c.execute("BEGIN IMMEDIATE"); now=utc_now_iso()
                c.execute("INSERT OR IGNORE INTO owner_memory_settings VALUES(?,?,?,?,?,?)",(owner,1,1,1,0,now))
                settings=c.execute("SELECT capture_enabled,governance_revision FROM owner_memory_settings WHERE owner_id=?",(owner,)).fetchone()
                if not settings["capture_enabled"] or settings["governance_revision"] != governance_revision:
                    c.rollback(); return False
                if revision is not None:
                    session = self._session(c, owner, session_key)
                    if session is None or session["current_generation_id"] != generation_id or session["current_revision"] != revision:
                        c.rollback(); return False
                for control_index, x in enumerate(controls):
                    _validate_control_timestamps(x)
                    fp = hashlib.sha256(json.dumps([x.subject_key, x.topic, x.value], sort_keys=True, default=str).encode()).hexdigest()
                    target = None
                    if x.memory_id:
                        target = c.execute("SELECT id,status FROM memories WHERE owner_id=? AND id=?", (owner, x.memory_id)).fetchone()
                        if target is None:
                            continue
                    elif x.action != "add":
                        if x.action == "upsert":
                            target = c.execute("SELECT id,status FROM memories WHERE owner_id=? AND subject_key=? AND topic=? AND status IN ('active','archived','disabled_by_agent') ORDER BY updated_at DESC,id DESC LIMIT 1", (owner, x.subject_key, x.topic)).fetchone()
                        else:
                            target = c.execute("SELECT id,status FROM memories WHERE owner_id=? AND fingerprint=? AND status IN ('active','archived','disabled_by_agent') ORDER BY updated_at DESC,id DESC LIMIT 1", (owner, fp)).fetchone()
                    disabled = c.execute("SELECT 1 FROM memories WHERE owner_id=? AND fingerprint=? AND status='disabled_by_user'", (owner, fp)).fetchone()
                    if disabled or (target and target["status"] == "disabled_by_user"):
                        continue
                    epistemic = {"current_message": "user_stated", "session_message": "session_derived", "tool_observation": "tool_derived", "assistant_conclusion": "assistant_conclusion", "inference": "inferred"}
                    # The highest qualification requirement governs mixed evidence.
                    kind = max((epistemic[source.source_kind] for source in x.sources), key=("user_stated", "session_derived", "tool_derived", "assistant_conclusion", "inferred").index, default="inferred")
                    duplicate = c.execute("SELECT id FROM memories WHERE owner_id=? AND fingerprint=? AND status IN ('active','archived','disabled_by_agent','provisional')", (owner, fp)).fetchone()
                    if duplicate and target is None:
                        continue
                    mid = _id("memory", owner, session_key, generation_id, segment_index, control_index, x.action, fp)
                    c.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (mid, owner, x.kind, x.subject_key, x.topic, json.dumps(x.value, sort_keys=True, default=str), x.statement, x.statement, fp, kind, "provisional", x.confidence, x.salience, x.surface_scope, generation_id, session_key, governance_revision, x.action, target["id"] if target else None, x.valid_from, x.valid_until, now, now))
                    for s in x.sources:c.execute("INSERT OR REPLACE INTO memory_sources VALUES(?,?,?,?,?)",(mid,s.source_kind,s.source_id,s.observed_at,int(s.available)))
                c.commit(); return True
        except sqlite3.OperationalError as e: raise self._safe(e) from None
    def search_memories(self,q):
        if not self.memory_settings(q.owner_id).retrieval_enabled:return ()
        limit=max(1,min(q.limit,50))
        try:
            with self._connection() as c:
                if q.text and q.text.strip(): rows=c.execute("SELECT m.*, -bm25(memories_fts) score FROM memories_fts JOIN memories m ON m.id=memories_fts.record_id WHERE memories_fts MATCH ? AND m.owner_id=? AND m.status IN ('active','archived') ORDER BY score DESC,m.salience DESC,m.confidence DESC,m.updated_at DESC LIMIT ?",(_fts(q.text),q.owner_id,limit)).fetchall()
                else: rows=c.execute("SELECT *,0 score FROM memories WHERE owner_id=? AND status IN ('active','archived') ORDER BY salience DESC,confidence DESC,updated_at DESC LIMIT ?",(q.owner_id,limit)).fetchall()
                return tuple(MemorySearchHit(self._record(c,r),float(r["score"])) for r in rows)
        except (sqlite3.OperationalError,ValueError) as e: raise StorageSearchError("memory_search_unavailable") from None
    def memory_context(self,q):
        if not self.memory_settings(q.owner_id).retrieval_enabled:return ()
        with self._connection() as c:
            rows=list(c.execute("SELECT * FROM memories WHERE owner_id=? AND status='active' AND surface_scope='always' ORDER BY salience DESC,confidence DESC,updated_at DESC LIMIT ?",(q.owner_id,max(0,min(q.always_limit,16)))))
            if q.text.strip():
                try: rows += [h.memory for h in self.search_memories(MemorySearchQuery(q.owner_id,q.text,max(0,min(q.contextual_limit,8)))) if h.memory.status=="active" and h.memory.surface_scope=="contextual"]
                except StorageSearchError: pass
            result=[]; used=0
            for x in rows:
                r=x if isinstance(x,MemoryRecord) else self._record(c,x)
                if r.id not in {v.id for v in result} and used+len(r.statement)<=q.total_chars: result.append(r);used+=len(r.statement)
            return tuple(result)
    def pending_memory_actions(self, owner, session_key):
        try:
            with self._connection() as c:
                rows=c.execute("SELECT * FROM pending_memory_actions WHERE owner_id=? AND session_key=? AND expires_at>? ORDER BY created_at",(owner,session_key,utc_now_iso())).fetchall()
                result=[]
                for row in rows:
                    ids=tuple(json.loads(row["target_ids_json"]))
                    if not ids: continue
                    marks=",".join("?"*len(ids))
                    targets=c.execute("SELECT statement FROM memories WHERE owner_id=? AND id IN (%s) ORDER BY id" % marks,(owner,*ids)).fetchall()
                    result.append(PendingMemoryAction(row["id"],ids,tuple(target["statement"][:240] for target in targets)))
                return tuple(result)
        except (sqlite3.OperationalError, ValueError) as e: raise StorageSearchError("pending_memory_actions_unavailable") from None
    def search_sessions(self,q):
        if not self.memory_settings(q.owner_id).retrieval_enabled:return ()
        if not q.text.strip(): raise StorageSearchError("session_search_query_required")
        if not q.roles: return ()
        limit=max(1,min(q.limit,20)); before=max(0,min(q.context_before,10)); after=max(0,min(q.context_after,10)); remaining=4000
        try:
            with self._connection() as c:
                rows=c.execute("SELECT m.*,s.session_key,s.connector,-bm25(messages_fts) score FROM messages_fts JOIN messages m ON m.id=messages_fts.record_id JOIN sessions s ON s.id=m.session_id WHERE messages_fts MATCH ? AND s.owner_id=? AND m.role IN (%s) AND (m.role='user' OR m.generation_status='accepted') ORDER BY score DESC LIMIT ?" % ",".join("?"*len(q.roles)),(_fts(q.text),q.owner_id,*q.roles,limit)).fetchall(); out=[]
                for r in rows:
                    context=c.execute("SELECT * FROM messages WHERE session_id=? AND ordinal BETWEEN ? AND ? AND (role='user' OR generation_status='accepted') ORDER BY ordinal,id",(r["session_id"],(r["ordinal"] or 0)-before,(r["ordinal"] or 0)+after)).fetchall()
                    texts=[r["content"], *(x["content"] for x in context)]
                    size=sum(len(text) for text in texts)
                    if size > remaining:
                        continue
                    remaining -= size
                    out.append(SessionSearchHit(r["session_id"],r["session_key"],r["connector"],r["id"],r["role"],r["content"],r["timestamp"],float(r["score"]),tuple(StoredMessage(x["role"],x["content"],x["timestamp"],json.loads(x["metadata_json"])) for x in context)))
                return tuple(out)
        except (sqlite3.OperationalError,ValueError) as e: raise StorageSearchError("session_search_unavailable") from None
    def apply_user_memory_control(self,owner,session_key,control):
        if isinstance(control,OwnerSettingControl): self.update_memory_setting(owner,control.setting,control.enabled); return MemoryGovernanceResult("applied")
        if isinstance(control,MemoryGovernanceControl):
            if not control.target_memory_ids: return MemoryGovernanceResult("not_found")
            try:
                with self._connection() as c:
                    c.execute("BEGIN IMMEDIATE"); now=utc_now_iso(); marks=",".join("?"*len(control.target_memory_ids))
                    rows=c.execute("SELECT id FROM memories WHERE owner_id=? AND id IN (%s)" % marks,(owner,*control.target_memory_ids)).fetchall()
                    if len(rows) != len(set(control.target_memory_ids)):
                        c.rollback(); return MemoryGovernanceResult("not_found")
                    targets=tuple(r["id"] for r in rows)
                    if control.action == "disable_by_user":
                        c.execute("UPDATE memories SET status='disabled_by_user',updated_at=? WHERE owner_id=? AND id IN (%s)" % marks,(now,owner,*targets))
                        c.execute("INSERT OR IGNORE INTO owner_memory_settings VALUES(?,?,?,?,?,?)",(owner,1,1,1,0,now))
                        c.execute("UPDATE owner_memory_settings SET governance_revision=governance_revision+1,updated_at=? WHERE owner_id=?",(now,owner)); c.commit()
                        self._checkpoint()
                        return MemoryGovernanceResult("applied",targets)
                    pending_id=str(uuid.uuid4())
                    # ISO timestamps sort chronologically, so expiry can be enforced in SQL.
                    from datetime import datetime, timedelta, timezone
                    expires=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
                    c.execute("INSERT INTO pending_memory_actions VALUES(?,?,?,?,?,?,?)",(pending_id,owner,session_key,"delete",json.dumps(targets),expires,now)); c.commit()
                    self._checkpoint()
                    return MemoryGovernanceResult("pending_confirmation",targets,pending_id)
            except sqlite3.OperationalError as e: raise self._safe(e) from None
        if isinstance(control,PendingMemoryActionControl):
            try:
                with self._connection() as c:
                    c.execute("BEGIN IMMEDIATE"); now=utc_now_iso()
                    pending=c.execute("SELECT * FROM pending_memory_actions WHERE id=? AND owner_id=? AND session_key=?",(control.pending_action_id,owner,session_key)).fetchone()
                    if not pending:
                        c.rollback()
                        if control.action == "confirm_delete":
                            self._checkpoint(scrub=True)
                        return MemoryGovernanceResult("not_found")
                    if pending["expires_at"] <= now:
                        c.execute("DELETE FROM pending_memory_actions WHERE id=?",(pending["id"],)); c.commit(); self._checkpoint(); return MemoryGovernanceResult("rejected")
                    targets=tuple(json.loads(pending["target_ids_json"]))
                    if control.action == "cancel_delete":
                        c.execute("DELETE FROM pending_memory_actions WHERE id=?",(pending["id"],)); c.commit(); self._checkpoint(); return MemoryGovernanceResult("cancelled",targets)
                    rows=c.execute("SELECT fingerprint FROM memories WHERE owner_id=? AND id IN (%s)" % ",".join("?"*len(targets)),(owner,*targets)).fetchall()
                    fingerprints=tuple(r["fingerprint"] for r in rows)
                    c.execute("DELETE FROM memories WHERE owner_id=? AND id IN (%s)" % ",".join("?"*len(targets)),(owner,*targets))
                    if fingerprints:
                        c.execute("DELETE FROM memories WHERE owner_id=? AND status='provisional' AND fingerprint IN (%s)" % ",".join("?"*len(fingerprints)),(owner,*fingerprints))
                    c.execute("INSERT OR IGNORE INTO memory_deletion_tombstones VALUES(?,?,?,?)",(_id("delete",owner,pending["id"]),owner,now,pending["id"]))
                    c.execute("DELETE FROM pending_memory_actions WHERE id=?",(pending["id"],)); c.execute("INSERT OR IGNORE INTO owner_memory_settings VALUES(?,?,?,?,?,?)",(owner,1,1,1,0,now)); c.execute("UPDATE owner_memory_settings SET governance_revision=governance_revision+1,updated_at=? WHERE owner_id=?",(now,owner)); c.commit()
                    self._checkpoint(scrub=True)
                    return MemoryGovernanceResult("applied",targets)
            except sqlite3.OperationalError as e: raise self._safe(e) from None
        return MemoryGovernanceResult("rejected")
    def delete_session(self,owner,session_id,cascade_memories=False):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); row=c.execute("SELECT id FROM sessions WHERE id=? AND owner_id=?",(session_id,owner)).fetchone()
            if row:
                references="SELECT id FROM messages WHERE session_id=? UNION SELECT connector_message_id FROM messages WHERE session_id=? AND connector_message_id IS NOT NULL"
                if cascade_memories:c.execute(f"DELETE FROM memories WHERE owner_id=? AND id IN (SELECT memory_id FROM memory_sources WHERE source_id IN ({references}))",(owner,session_id,session_id))
                else:c.execute(f"UPDATE memory_sources SET source_available=0 WHERE source_id IN ({references})",(session_id,session_id))
                c.execute("DELETE FROM sessions WHERE id=?",(session_id,))
            c.commit()
        self._checkpoint(scrub=True)
    def delete_owner(self,owner):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE"); c.execute("DELETE FROM sessions WHERE owner_id=?",(owner,)); c.execute("DELETE FROM memories WHERE owner_id=?",(owner,)); c.execute("DELETE FROM owner_memory_settings WHERE owner_id=?",(owner,)); c.execute("DELETE FROM pending_memory_actions WHERE owner_id=?",(owner,)); c.execute("DELETE FROM memory_deletion_tombstones WHERE owner_id=?",(owner,)); c.commit()
        self._checkpoint(scrub=True)

    @_checkpoint_after_write
    def rebuild_index(self, owner_id=None):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            if owner_id is None:
                c.execute("DELETE FROM messages_fts"); c.execute("DELETE FROM memories_fts")
                c.execute("INSERT INTO messages_fts SELECT m.id,s.owner_id,m.content FROM messages m JOIN sessions s ON s.id=m.session_id")
                c.execute("INSERT INTO memories_fts SELECT id,owner_id,search_text FROM memories")
            else:
                c.execute("DELETE FROM messages_fts WHERE owner_id=?",(owner_id,)); c.execute("DELETE FROM memories_fts WHERE owner_id=?",(owner_id,))
                c.execute("INSERT INTO messages_fts SELECT m.id,s.owner_id,m.content FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.owner_id=?",(owner_id,))
                c.execute("INSERT INTO memories_fts SELECT id,owner_id,search_text FROM memories WHERE owner_id=?",(owner_id,))
            c.commit()

    def integrity_check(self):
        with self._connection() as c:
            return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and not c.execute("PRAGMA foreign_key_check").fetchone()

    @_checkpoint_after_write
    def prune_provisional_artifacts(self, older_than):
        with self._connection() as c:
            c.execute("BEGIN IMMEDIATE")
            count=c.execute("DELETE FROM memories WHERE status='provisional' AND created_at<?",(older_than,)).rowcount
            c.execute("DELETE FROM pending_memory_actions WHERE expires_at<?",(utc_now_iso(),)); c.commit()
            return count

    # Transfer methods intentionally use stable canonical rows, never sqlite dump syntax.
    def export_owner_records(self, owner):
        with self._connection() as c:
            records=[]
            session_ids=[r[0] for r in c.execute("SELECT id FROM sessions WHERE owner_id=?",(owner,))]
            for table, where, args in (("sessions","owner_id=?",(owner,)),("memories","owner_id=?",(owner,)),("owner_memory_settings","owner_id=?",(owner,)),("pending_memory_actions","owner_id=? AND expires_at>?",(owner,utc_now_iso())),("memory_deletion_tombstones","owner_id=?",(owner,))):
                records += [{"table":table,"row":dict(r)} for r in c.execute(f"SELECT * FROM {table} WHERE {where}",args)]
            if session_ids:
                marks=",".join("?"*len(session_ids)); records += [{"table":"messages","row":dict(r)} for r in c.execute(f"SELECT * FROM messages WHERE session_id IN ({marks})",session_ids)]
                records += [{"table":"accepted_generations","row":dict(r)} for r in c.execute(f"SELECT * FROM accepted_generations WHERE session_id IN ({marks})",session_ids)]
            memory_ids=[r[0] for r in c.execute("SELECT id FROM memories WHERE owner_id=?",(owner,))]
            if memory_ids:
                marks=",".join("?"*len(memory_ids)); records += [{"table":"memory_sources","row":dict(r)} for r in c.execute(f"SELECT * FROM memory_sources WHERE memory_id IN ({marks})",memory_ids)]
            return records
    @_checkpoint_after_write
    def import_owner_records(self, owner, records):
        # SQLite validates references transactionally; materialize only at this
        # adapter boundary while callers and alternate adapters stream records.
        records=list(records)
        permitted={"sessions","messages","accepted_generations","memories","memory_sources","owner_memory_settings","pending_memory_actions","memory_deletion_tombstones"}
        if any(not isinstance(x,dict) or x.get("table") not in permitted or not isinstance(x.get("row"),dict) for x in records): raise ValueError("malformed canonical record")
        try:
            with self._connection() as c:
                c.execute("BEGIN IMMEDIATE")
                incoming_sessions={x["row"].get("id") for x in records if x["table"]=="sessions"}
                incoming_memories={x["row"].get("id") for x in records if x["table"]=="memories"}
                owned_sessions={x[0] for x in c.execute("SELECT id FROM sessions WHERE owner_id=?",(owner,))}
                owned_memories={x[0] for x in c.execute("SELECT id FROM memories WHERE owner_id=?",(owner,))}
                for item in records:
                    row=item["row"]; table=item["table"]
                    if table in {"messages","accepted_generations"} and row.get("session_id") not in incoming_sessions | owned_sessions: raise ValueError("cross-owner canonical record")
                    if table=="memory_sources" and row.get("memory_id") not in incoming_memories | owned_memories: raise ValueError("cross-owner canonical record")
                for table in ("sessions","messages","accepted_generations","memories","memory_sources","owner_memory_settings","pending_memory_actions","memory_deletion_tombstones"):
                    for item in (x for x in records if x["table"]==table):
                        row=item["row"]
                        if table in {"sessions","memories","owner_memory_settings","pending_memory_actions","memory_deletion_tombstones"} and row.get("owner_id")!=owner: raise ValueError("cross-owner canonical record")
                        cols=tuple(row); c.execute(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",tuple(row[x] for x in cols))
                c.commit()
        except Exception:
            try: c.rollback()
            except Exception: pass
            raise
