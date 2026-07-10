"""Create one immutable Daytona snapshot from the checked-in Dockerfile."""

from __future__ import annotations

import argparse
import sys

from daytona import CreateSnapshotParams, Daytona, Image
from daytona.common.errors import DaytonaNotFoundError


def _existing_snapshot(daytona: Daytona, name: str):
    try:
        return daytona.snapshot.get(name)
    except DaytonaNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="create a named Daytona snapshot")
    parser.add_argument("--name", required=True, help="immutable snapshot name")
    parser.add_argument("--replace", action="store_true", help="replace an existing snapshot with this name")
    args = parser.parse_args(argv)

    try:
        daytona = Daytona()
        existing = _existing_snapshot(daytona, args.name)
        if existing is not None:
            if not args.replace:
                print("snapshot already exists; rerun with --replace", file=sys.stderr)
                return 1
            daytona.snapshot.delete(existing)

        image = Image.from_dockerfile("Dockerfile.daytona")
        snapshot = daytona.snapshot.create(CreateSnapshotParams(name=args.name, image=image))
    except Exception:
        print("snapshot creation failed", file=sys.stderr)
        return 1

    if getattr(snapshot, "state", "").lower() != "active":
        print("snapshot did not become active", file=sys.stderr)
        return 1

    print("active")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
