from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxRecord:
    tomo_id: str
    sandbox_id: str | None
    sandbox_name: str
    volume_name: str
    snapshot: str
    status: str
    error_code: str | None
    updated_at: int


class SandboxRegistry:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "sandbox_registry.sqlite"
        self._migrate()

    def upsert(
        self,
        tomo_id: str,
        sandbox_id: str | None,
        snapshot: str,
        status: str,
        error_code: str | None = None,
        updated_at: int | None = None,
    ) -> SandboxRecord:
        record = SandboxRecord(
            tomo_id=tomo_id,
            sandbox_id=sandbox_id,
            sandbox_name=self.sandbox_name_for(tomo_id),
            volume_name=self.volume_name_for(tomo_id),
            snapshot=snapshot,
            status=status,
            error_code=error_code,
            updated_at=int(time.time()) if updated_at is None else updated_at,
        )
        db = self._connect()
        try:
            db.execute(
                """
                insert into sandboxes(
                  tomo_id, sandbox_id, sandbox_name, volume_name, snapshot, status, error_code, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(tomo_id) do update set
                  sandbox_id = excluded.sandbox_id,
                  sandbox_name = excluded.sandbox_name,
                  volume_name = excluded.volume_name,
                  snapshot = excluded.snapshot,
                  status = excluded.status,
                  error_code = excluded.error_code,
                  updated_at = excluded.updated_at
                """,
                (
                    record.tomo_id,
                    record.sandbox_id,
                    record.sandbox_name,
                    record.volume_name,
                    record.snapshot,
                    record.status,
                    record.error_code,
                    record.updated_at,
                ),
            )
            db.commit()
        finally:
            db.close()
        return record

    def get(self, tomo_id: str) -> SandboxRecord | None:
        db = self._connect()
        try:
            row = db.execute(
                """
                select tomo_id, sandbox_id, sandbox_name, volume_name, snapshot, status, error_code, updated_at
                from sandboxes where tomo_id = ?
                """,
                (tomo_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            return None
        return SandboxRecord(
            tomo_id=row["tomo_id"],
            sandbox_id=row["sandbox_id"],
            sandbox_name=row["sandbox_name"],
            volume_name=row["volume_name"],
            snapshot=row["snapshot"],
            status=row["status"],
            error_code=row["error_code"],
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def sandbox_name_for(tomo_id: str) -> str:
        return f"tomo-sandbox-{SandboxRegistry._name_suffix(tomo_id)}"

    @staticmethod
    def volume_name_for(tomo_id: str) -> str:
        return f"tomo-volume-{SandboxRegistry._name_suffix(tomo_id)}"

    def _migrate(self) -> None:
        db = self._connect()
        try:
            db.execute(
                """
                create table if not exists sandboxes(
                  tomo_id text primary key,
                  sandbox_id text,
                  sandbox_name text not null,
                  volume_name text not null,
                  snapshot text not null,
                  status text not null,
                  error_code text,
                  updated_at integer not null
                )
                """
            )
            db.commit()
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _name_suffix(tomo_id: str) -> str:
        return hashlib.sha256(tomo_id.encode("utf-8")).hexdigest()[:16]
