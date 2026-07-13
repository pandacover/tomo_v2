"""Versioned, backend-neutral JSONL transfer helpers.

Adapters opting into transfer expose `export_owner_records` and
`import_owner_records`; the public repository protocol deliberately does not
expose tables or transactions.
"""
from __future__ import annotations

import json
from typing import Iterable, TextIO

from .personal_data import PersonalDataTransferRepository

FORMAT_VERSION = 1


def export_owner(repository: PersonalDataTransferRepository, owner_id: str, output: TextIO) -> None:
    output.write(json.dumps({"format": "tomo_personal_data", "version": FORMAT_VERSION, "owner_id": owner_id}, sort_keys=True) + "\n")
    for record in repository.export_owner_records(owner_id):
        output.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")


def import_owner(repository: PersonalDataTransferRepository, owner_id: str, source: Iterable[str]) -> None:
    iterator = iter(source)
    try:
        header_line = next(iterator)
    except StopIteration:
        raise ValueError("canonical stream is empty")
    try:
        header = json.loads(header_line)
        if not isinstance(header, dict):
            raise ValueError("malformed canonical stream")
        if header != {"format": "tomo_personal_data", "version": FORMAT_VERSION, "owner_id": owner_id}:
            raise ValueError("unsupported or cross-owner canonical stream")
        records = (json.loads(line) for line in iterator if line.strip())
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("malformed canonical stream") from error
    def validated_records():
        try:
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("malformed canonical stream")
                if record.get("owner_id", owner_id) != owner_id:
                    raise ValueError("cross-owner canonical record")
                yield record
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("malformed canonical stream") from error
    repository.import_owner_records(owner_id, validated_records())
