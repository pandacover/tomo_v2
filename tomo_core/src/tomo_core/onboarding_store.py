from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallLink:
    token: str
    dm_url: str
    browser_url: str
    expires_at: int


@dataclass(frozen=True)
class TelegramInstallation:
    user_id: str
    tomo_id: str
    chat_id: str
    actor_id: str
    installed_at: int


@dataclass(frozen=True)
class TelegramInboxUpdate:
    update_id: int
    chat_id: str
    payload: str
    status: str
    attempts: int
    available_at: int
    error_code: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class EnqueueResult:
    enqueued: bool
    burst_id: str | None = None
    revision: int | None = None
    superseded_generation_id: str | None = None
    superseded_session_id: str | None = None

    def __bool__(self) -> bool:
        return self.enqueued


@dataclass(frozen=True)
class TelegramGenerationInput:
    update_id: int
    ordinal: int
    payload: str
    message_id: str | None
    telegram_sent_at: float | None


@dataclass(frozen=True)
class TelegramGenerationWork:
    generation_id: str
    burst_id: str
    chat_id: str
    tomo_id: str
    revision: int
    session_id: str
    inputs: tuple[TelegramGenerationInput, ...]
    visible_assistant_utterances: tuple[str, ...] = ()
    accepted_generation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelegramControlWork:
    update: TelegramInboxUpdate


@dataclass(frozen=True)
class InterruptedGeneration:
    generation_id: str
    chat_id: str
    tomo_id: str
    revision: int
    session_id: str


class TelegramOnboardingStore:
    def __init__(self, data_dir: str | Path, *, input_debounce_seconds: float = 0.7) -> None:
        if input_debounce_seconds < 0:
            raise ValueError("input_debounce_seconds must be non-negative")
        self.input_debounce_seconds = input_debounce_seconds
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "onboarding.sqlite"
        self._init_db()

    def create_install_link(self, user_id: str, bot_username: str, ttl_seconds: int = 600) -> InstallLink:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + ttl_seconds
        tomo_id = self._tomo_id_for_user(user_id)
        db = self._connect()
        db.execute(
            """
            insert into telegram_install_tokens(token_hash, user_id, tomo_id, expires_at, consumed_at, created_at)
            values (?, ?, ?, ?, null, ?)
            """,
            (self._hash(token), user_id, tomo_id, expires_at, now),
        )
        db.commit()
        db.close()
        return InstallLink(
            token=token,
            dm_url=f"tg://resolve?domain={bot_username}&start={token}",
            browser_url=f"https://t.me/{bot_username}?start={token}",
            expires_at=expires_at,
        )

    def consume_start_token(self, token: str, chat_id: str, actor_id: str) -> TelegramInstallation | None:
        now = int(time.time())
        token_hash = self._hash(token)
        db = self._connect()
        try:
            row = db.execute(
                """
                select user_id, tomo_id, expires_at, consumed_at from telegram_install_tokens
                where token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None or row["consumed_at"] is not None or int(row["expires_at"]) < now:
                db.close()
                return None
            db.execute("update telegram_install_tokens set consumed_at = ? where token_hash = ?", (now, token_hash))
            db.execute(
                """
                insert into telegram_installations(user_id, tomo_id, chat_id, actor_id, installed_at)
                values (?, ?, ?, ?, ?)
                on conflict(chat_id) do update set
                  user_id = excluded.user_id,
                  tomo_id = excluded.tomo_id,
                  actor_id = excluded.actor_id,
                  installed_at = excluded.installed_at
                """,
                (row["user_id"], row["tomo_id"], chat_id, actor_id, now),
            )
            db.commit()
            installation = TelegramInstallation(row["user_id"], row["tomo_id"], chat_id, actor_id, now)
            return installation
        finally:
            db.close()

    def installation_for_chat(self, chat_id: str) -> TelegramInstallation | None:
        db = self._connect()
        try:
            row = db.execute(
                "select user_id, tomo_id, chat_id, actor_id, installed_at from telegram_installations where chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return TelegramInstallation(row["user_id"], row["tomo_id"], row["chat_id"], row["actor_id"], int(row["installed_at"]))
        finally:
            db.close()

    def enqueue_update(
        self,
        update_id: int,
        chat_id: str,
        payload: str,
        *,
        now: float | None = None,
        update_kind: str = "control",
        message_id: str | None = None,
        telegram_sent_at: float | None = None,
        tomo_id: str = "",
    ) -> EnqueueResult:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            result = db.execute(
                """
                insert into telegram_inbox(
                  update_id, chat_id, payload, status, attempts, available_at, error_code, created_at, updated_at,
                  update_kind, message_id, telegram_sent_at, burst_id, tomo_id
                ) values (?, ?, ?, 'pending', 0, ?, null, ?, ?, ?, ?, ?, null, ?)
                on conflict(update_id) do nothing
                """,
                (update_id, chat_id, payload, now, now, now, update_kind, message_id, telegram_sent_at, tomo_id),
            )
            if result.rowcount != 1:
                db.commit()
                return EnqueueResult(False)
            if update_kind != "message":
                db.commit()
                return EnqueueResult(True)

            superseded_generation_id = None
            superseded_session_id = None
            turn = db.execute("select * from telegram_chat_turns where chat_id = ?", (chat_id,)).fetchone()
            if turn is None or turn["burst_id"] is None:
                burst_id = f"{chat_id}:{update_id}"
                revision = 1
                db.execute(
                    """
                    insert into telegram_chat_turns(chat_id, burst_id, revision, quiet_until, active_generation_id, updated_at)
                    values (?, ?, ?, ?, null, ?)
                    on conflict(chat_id) do update set burst_id=excluded.burst_id, revision=excluded.revision,
                      quiet_until=excluded.quiet_until, active_generation_id=null, updated_at=excluded.updated_at
                    """,
                    (chat_id, burst_id, revision, float(now) + self.input_debounce_seconds, now),
                )
            else:
                burst_id = turn["burst_id"]
                revision = int(turn["revision"]) + 1
                active_generation_id = turn["active_generation_id"]
                if active_generation_id:
                    generation = db.execute(
                        "select session_id from telegram_generations where generation_id = ? and status = 'active'",
                        (active_generation_id,),
                    ).fetchone()
                    db.execute(
                        "update telegram_generations set status = 'superseded', updated_at = ? where generation_id = ? and status = 'active'",
                        (now, active_generation_id),
                    )
                    superseded_generation_id = active_generation_id
                    superseded_session_id = generation["session_id"] if generation is not None else None
                db.execute(
                    """
                    update telegram_chat_turns
                    set revision = ?, quiet_until = ?, active_generation_id = null, updated_at = ?
                    where chat_id = ?
                    """,
                    (revision, float(now) + self.input_debounce_seconds, now, chat_id),
                )
            db.execute("update telegram_inbox set burst_id = ? where update_id = ?", (burst_id, update_id))
            db.commit()
            return EnqueueResult(True, burst_id, revision, superseded_generation_id, superseded_session_id)
        finally:
            db.close()

    def claim_next_work(self, *, now: float | None = None) -> TelegramControlWork | TelegramGenerationWork | None:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            turn = db.execute(
                """
                select * from telegram_chat_turns
                where burst_id is not null and active_generation_id is null and quiet_until <= ?
                order by quiet_until, chat_id limit 1
                """,
                (now,),
            ).fetchone()
            if turn is None:
                db.commit()
                return None
            rows = db.execute(
                """
                select * from telegram_inbox
                where chat_id = ? and burst_id = ? and update_kind = 'message' and status != 'completed'
                order by update_id
                """,
                (turn["chat_id"], turn["burst_id"]),
            ).fetchall()
            if not rows:
                db.commit()
                return None
            revision = int(turn["revision"])
            burst_id = turn["burst_id"]
            attempt = int(
                db.execute(
                    "select count(*) from telegram_generations where burst_id = ? and revision = ?",
                    (burst_id, revision),
                ).fetchone()[0]
            ) + 1
            generation_id = f"{burst_id}:r{revision}" if attempt == 1 else f"{burst_id}:r{revision}:a{attempt}"
            session_id = self._session_id(generation_id)
            tomo_id = rows[-1]["tomo_id"] or ""
            db.execute(
                """
                insert into telegram_generations(generation_id, burst_id, chat_id, tomo_id, revision, session_id, status, error_code, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, 'active', null, ?, ?)
                """,
                (generation_id, burst_id, turn["chat_id"], tomo_id, revision, session_id, now, now),
            )
            inputs = []
            for ordinal, row in enumerate(rows, start=1):
                db.execute(
                    "insert or ignore into telegram_generation_inputs(generation_id, update_id, ordinal) values (?, ?, ?)",
                    (generation_id, row["update_id"], ordinal),
                )
                inputs.append(
                    TelegramGenerationInput(
                        update_id=int(row["update_id"]),
                        ordinal=ordinal,
                        payload=row["payload"],
                        message_id=row["message_id"],
                        telegram_sent_at=row["telegram_sent_at"],
                    )
                )
            visible_rows = db.execute(
                """
                select e.generation_id, e.text
                from telegram_delivery_events e
                join telegram_generations g on g.generation_id = e.generation_id
                where g.burst_id = ? and e.status in ('sent', 'unknown')
                order by g.created_at, e.sequence
                """,
                (turn["burst_id"],),
            ).fetchall()
            visible = tuple(str(row["text"]) for row in visible_rows)
            completed_rows = db.execute(
                "select generation_id from telegram_generations where chat_id = ? and status = 'completed' order by created_at",
                (turn["chat_id"],),
            ).fetchall()
            accepted = tuple(
                dict.fromkeys(
                    [str(row["generation_id"]) for row in completed_rows]
                )
            )
            db.execute("update telegram_chat_turns set active_generation_id = ?, updated_at = ? where chat_id = ?", (generation_id, now, turn["chat_id"]))
            db.commit()
            return TelegramGenerationWork(generation_id, burst_id, turn["chat_id"], tomo_id, revision, session_id, tuple(inputs), visible, accepted)
        finally:
            db.close()

    def is_generation_active(self, generation_id: str, revision: int) -> bool:
        db = self._connect()
        try:
            row = db.execute(
                "select 1 from telegram_generations where generation_id = ? and revision = ? and status = 'active'",
                (generation_id, revision),
            ).fetchone()
            return row is not None
        finally:
            db.close()

    def reserve_delivery(self, generation_id: str, revision: int, sequence: int, move: str, text: str, reply_to_message_id: str | None, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            active = db.execute(
                "select 1 from telegram_generations where generation_id = ? and revision = ? and status = 'active'",
                (generation_id, revision),
            ).fetchone()
            if active is None:
                db.commit()
                return False
            result = db.execute(
                """
                insert or ignore into telegram_delivery_events(generation_id, sequence, move, text, reply_to_message_id, status, telegram_message_id, created_at, updated_at)
                values (?, ?, ?, ?, ?, 'reserved', null, ?, ?)
                """,
                (generation_id, sequence, move, text, reply_to_message_id, now, now),
            )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def mark_delivery_sent(self, generation_id: str, sequence: int, telegram_message_id: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            result = db.execute(
                "update telegram_delivery_events set status = 'sent', telegram_message_id = ?, updated_at = ? where generation_id = ? and sequence = ? and status = 'reserved'",
                (telegram_message_id, now, generation_id, sequence),
            )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def mark_delivery_unknown(self, generation_id: str, sequence: int, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            result = db.execute(
                "update telegram_delivery_events set status = 'unknown', updated_at = ? where generation_id = ? and sequence = ? and status = 'reserved'",
                (now, generation_id, sequence),
            )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def mark_delivery_suppressed(self, generation_id: str, sequence: int, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            result = db.execute(
                "update telegram_delivery_events set status = 'suppressed', updated_at = ? where generation_id = ? and sequence = ? and status = 'reserved'",
                (now, generation_id, sequence),
            )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def delivery_status(self, generation_id: str, sequence: int) -> str | None:
        db = self._connect()
        try:
            row = db.execute(
                "select status from telegram_delivery_events where generation_id = ? and sequence = ?",
                (generation_id, sequence),
            ).fetchone()
            return None if row is None else str(row["status"])
        finally:
            db.close()

    def complete_generation(self, generation_id: str, revision: int, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            row = db.execute(
                "select burst_id, chat_id from telegram_generations where generation_id = ? and revision = ? and status = 'active'",
                (generation_id, revision),
            ).fetchone()
            if row is None:
                db.commit()
                return False
            db.execute("update telegram_generations set status = 'completed', updated_at = ? where generation_id = ?", (now, generation_id))
            db.execute("update telegram_inbox set status = 'completed', updated_at = ? where burst_id = ?", (now, row["burst_id"]))
            db.execute(
                "update telegram_chat_turns set burst_id = null, active_generation_id = null, updated_at = ? where chat_id = ? and active_generation_id = ?",
                (now, row["chat_id"], generation_id),
            )
            db.commit()
            return True
        finally:
            db.close()

    def fail_generation(
        self,
        generation_id: str,
        error_code: str,
        *,
        now: float | None = None,
        max_attempts: int | None = None,
    ) -> bool:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            row = db.execute(
                "select burst_id, chat_id from telegram_generations where generation_id = ? and status = 'active'",
                (generation_id,),
            ).fetchone()
            if row is None:
                db.commit()
                return False
            result = db.execute(
                "update telegram_generations set status = 'failed', error_code = ?, updated_at = ? where generation_id = ? and status = 'active'",
                (self._safe_error_code(error_code), now, generation_id),
            )
            attempt_count = int(
                db.execute(
                    "select count(*) from telegram_generations where burst_id = ?",
                    (row["burst_id"],),
                ).fetchone()[0]
            )
            terminal = max_attempts is not None and attempt_count >= max_attempts
            if terminal:
                db.execute(
                    "update telegram_inbox set status = 'completed', error_code = ?, updated_at = ? where burst_id = ?",
                    (self._safe_error_code(error_code), now, row["burst_id"]),
                )
                db.execute(
                    """
                    update telegram_chat_turns
                    set burst_id = null, revision = 0, active_generation_id = null, quiet_until = ?, updated_at = ?
                    where chat_id = ? and active_generation_id = ?
                    """,
                    (now, now, row["chat_id"], generation_id),
                )
            else:
                retry_at = now + min(60.0, 2.0 ** max(0, attempt_count - 1)) if max_attempts is not None else now
                db.execute(
                    """
                    update telegram_chat_turns
                    set active_generation_id = null, quiet_until = ?, updated_at = ?
                    where chat_id = ? and active_generation_id = ?
                    """,
                    (retry_at, now, row["chat_id"], generation_id),
                )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def recover_interrupted_generations(self, *, now: float | None = None) -> tuple[InterruptedGeneration, ...]:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            rows = db.execute(
                "select generation_id, chat_id, tomo_id, revision, session_id from telegram_generations where status = 'active' order by created_at"
            ).fetchall()
            for row in rows:
                db.execute(
                    "update telegram_delivery_events set status = 'unknown', updated_at = ? where generation_id = ? and status = 'reserved'",
                    (now, row["generation_id"]),
                )
                db.execute(
                    "update telegram_generations set status = 'superseded', updated_at = ? where generation_id = ? and status = 'active'",
                    (now, row["generation_id"]),
                )
                db.execute(
                    """
                    update telegram_chat_turns
                    set active_generation_id = null, quiet_until = ?, updated_at = ?
                    where chat_id = ? and active_generation_id = ?
                    """,
                    (now, now, row["chat_id"], row["generation_id"]),
                )
            db.commit()
            return tuple(
                InterruptedGeneration(
                    row["generation_id"],
                    row["chat_id"],
                    row["tomo_id"],
                    int(row["revision"]),
                    row["session_id"],
                )
                for row in rows
            )
        finally:
            db.close()

    def claim_next_update(self, *, now: int | None = None) -> TelegramInboxUpdate | None:
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            row = db.execute(
                """
                select * from telegram_inbox as candidate
                where status = 'pending' and update_kind = 'control' and available_at <= ?
                  and not exists (
                    select 1 from telegram_inbox as active
                    where active.chat_id = candidate.chat_id and active.update_kind = 'control' and active.status = 'processing'
                  )
                  and not exists (
                    select 1 from telegram_inbox as earlier
                    where earlier.chat_id = candidate.chat_id
                      and earlier.update_kind = 'control'
                      and earlier.update_id < candidate.update_id
                      and earlier.status != 'completed'
                  )
                order by available_at, update_id
                limit 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                """
                update telegram_inbox
                set status = 'processing', attempts = attempts + 1, updated_at = ?
                where update_id = ?
                """,
                (now, row["update_id"]),
            )
            db.commit()
            return TelegramInboxUpdate(
                update_id=int(row["update_id"]),
                chat_id=row["chat_id"],
                payload=row["payload"],
                status="processing",
                attempts=int(row["attempts"]) + 1,
                available_at=int(row["available_at"]),
                error_code=row["error_code"],
                created_at=int(row["created_at"]),
                updated_at=now,
            )
        finally:
            db.close()

    def complete_update(self, update_id: int, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        db = self._connect()
        try:
            db.execute(
                """
                update telegram_inbox set status = 'completed', error_code = null, updated_at = ?
                where update_id = ? and status = 'processing'
                """,
                (now, update_id),
            )
            db.commit()
        finally:
            db.close()

    def retry_update(self, update_id: int, error_code: str, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        db = self._connect()
        try:
            row = db.execute(
                "select attempts from telegram_inbox where update_id = ? and status = 'processing'", (update_id,)
            ).fetchone()
            if row is not None:
                delay = min(300, 2 ** (int(row["attempts"]) - 1))
                db.execute(
                    """
                    update telegram_inbox
                    set status = 'pending', available_at = ?, error_code = ?, updated_at = ?
                    where update_id = ? and status = 'processing'
                    """,
                    (now + delay, self._safe_error_code(error_code), now, update_id),
                )
                db.commit()
        finally:
            db.close()

    def reset_interrupted_updates(self, *, now: int | None = None) -> int:
        now = int(time.time()) if now is None else now
        db = self._connect()
        try:
            result = db.execute(
                """
                update telegram_inbox set status = 'pending', available_at = ?, updated_at = ?
                where status = 'processing'
                """,
                (now, now),
            )
            db.commit()
            return result.rowcount
        finally:
            db.close()

    def _init_db(self) -> None:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            db.execute("""
                create table if not exists telegram_install_tokens(
                  token_hash text primary key,
                  user_id text not null,
                  tomo_id text not null,
                  expires_at integer not null,
                  consumed_at integer,
                  created_at integer not null
                )
            """)
            db.execute("""
                create table if not exists telegram_installations(
                  chat_id text primary key,
                  user_id text not null,
                  tomo_id text not null,
                  actor_id text not null,
                  installed_at integer not null
                )
            """)
            db.execute("""
                create table if not exists telegram_inbox(
                  update_id integer primary key,
                  chat_id text not null,
                  payload text not null,
                  status text not null check(status in ('pending', 'processing', 'completed')),
                  attempts integer not null default 0,
                  available_at integer not null,
                  error_code text,
                  created_at integer not null,
                  updated_at integer not null
                )
            """)
            self._ensure_columns(
                db,
                "telegram_inbox",
                {
                    "update_kind": "text not null default 'control'",
                    "message_id": "text",
                    "telegram_sent_at": "real",
                    "burst_id": "text",
                    "tomo_id": "text not null default ''",
                },
            )
            db.execute("""
                create index if not exists telegram_inbox_pending_idx
                on telegram_inbox(status, available_at, update_id)
            """)
            db.execute("""
                create table if not exists telegram_chat_turns(
                  chat_id text primary key,
                  burst_id text,
                  revision integer not null default 0,
                  quiet_until real not null default 0,
                  active_generation_id text,
                  updated_at real not null
                )
            """)
            db.execute("""
                create table if not exists telegram_generations(
                  generation_id text primary key,
                  burst_id text not null,
                  chat_id text not null,
                  tomo_id text not null,
                  revision integer not null,
                  session_id text not null,
                  status text not null check(status in ('active','superseded','completed','failed')),
                  error_code text,
                  created_at real not null,
                  updated_at real not null
                )
            """)
            db.execute("""
                create unique index if not exists telegram_one_active_generation_per_chat
                on telegram_generations(chat_id) where status = 'active'
            """)
            db.execute("""
                create table if not exists telegram_generation_inputs(
                  generation_id text not null,
                  update_id integer not null,
                  ordinal integer not null,
                  primary key(generation_id, update_id)
                )
            """)
            db.execute("""
                create table if not exists telegram_delivery_events(
                  generation_id text not null,
                  sequence integer not null,
                  move text not null,
                  text text not null,
                  reply_to_message_id text,
                  status text not null check(status in ('reserved','sent','unknown','suppressed')),
                  telegram_message_id text,
                  created_at real not null,
                  updated_at real not null,
                  primary key(generation_id, sequence)
                )
            """)
            db.commit()
        finally:
            db.close()

    def _ensure_columns(self, db, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in db.execute(f"pragma table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"alter table {table} add column {name} {definition}")

    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _tomo_id_for_user(user_id: str) -> str:
        suffix = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:10]
        safe_user = "".join(ch if ch.isalnum() else "-" for ch in user_id.lower()).strip("-")[:32] or "user"
        return f"tomo-{safe_user}-{suffix}"

    @staticmethod
    def _safe_error_code(error_code: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", error_code.lower()).strip("_")
        return normalized[:64] or "unknown_error"

    @staticmethod
    def _session_id(generation_id: str) -> str:
        digest = hashlib.sha256(generation_id.encode("utf-8")).hexdigest()[:24]
        return f"telegram-generation-{digest}"
