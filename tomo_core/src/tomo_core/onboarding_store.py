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


class TelegramOnboardingStore:
    def __init__(self, data_dir: str | Path) -> None:
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

    def enqueue_update(self, update_id: int, chat_id: str, payload: str, *, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        db = self._connect()
        try:
            result = db.execute(
                """
                insert into telegram_inbox(
                  update_id, chat_id, payload, status, attempts, available_at, error_code, created_at, updated_at
                ) values (?, ?, ?, 'pending', 0, ?, null, ?, ?)
                on conflict(update_id) do nothing
                """,
                (update_id, chat_id, payload, now, now, now),
            )
            db.commit()
            return result.rowcount == 1
        finally:
            db.close()

    def claim_next_update(self, *, now: int | None = None) -> TelegramInboxUpdate | None:
        now = int(time.time()) if now is None else now
        db = self._connect()
        try:
            db.execute("begin immediate")
            row = db.execute(
                """
                select * from telegram_inbox as candidate
                where status = 'pending' and available_at <= ?
                  and not exists (
                    select 1 from telegram_inbox as active
                    where active.chat_id = candidate.chat_id and active.status = 'processing'
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
            db.execute("""
                create index if not exists telegram_inbox_pending_idx
                on telegram_inbox(status, available_at, update_id)
            """)
        finally:
            db.close()

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
