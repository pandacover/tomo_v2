from __future__ import annotations

import argparse
import os
import time
from typing import Any, Protocol


DEFAULT_SNAPSHOT_NAME = "tomo-core-sandbox"


class SnapshotGateway(Protocol):
    def get(self, name: str) -> Any | None: ...

    def create(self, name: str, image: str) -> Any: ...

    def wait(self, snapshot: Any) -> Any: ...


def ensure_snapshot(
    gateway: SnapshotGateway,
    name: str,
    image: str,
    *,
    build: bool = False,
    wait: bool = False,
) -> Any:
    """Return a named snapshot, creating it only when --build permits it."""
    snapshot = gateway.get(name)
    if snapshot is None:
        if not build:
            raise RuntimeError(f"snapshot {name!r} does not exist; rerun with --build")
        snapshot = gateway.create(name, image)
    return gateway.wait(snapshot) if wait else snapshot


class DaytonaSnapshotGateway:
    """Small adapter around the synchronous Daytona SDK."""

    def __init__(self) -> None:
        from daytona import Daytona

        self._daytona = Daytona()

    def get(self, name: str) -> Any | None:
        try:
            return self._daytona.snapshot.get(name)
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                return None
            raise

    def create(self, name: str, image: str) -> Any:
        from daytona import CreateSnapshotParams

        return self._daytona.snapshot.create(CreateSnapshotParams(name=name, image=image), timeout=0)

    def wait(self, snapshot: Any) -> Any:
        while getattr(snapshot, "state", "active").lower() not in {"active", "ready"}:
            if getattr(snapshot, "state", "").lower() in {"error", "failed"}:
                raise RuntimeError(f"snapshot {snapshot.name!r} failed: {getattr(snapshot, 'error_reason', '')}")
            time.sleep(2)
            snapshot = self._daytona.snapshot.get(snapshot.name)
        return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="create or reuse the tomo Daytona sandbox snapshot")
    parser.add_argument("--name", default=os.getenv("TOMO_DAYTONA_SNAPSHOT_NAME", DEFAULT_SNAPSHOT_NAME))
    parser.add_argument("--image", default=os.getenv("TOMO_DAYTONA_IMAGE", "tomo-core-sandbox:latest"))
    parser.add_argument("--build", action="store_true", help="create the named snapshot when it is absent")
    parser.add_argument("--wait", action="store_true", help="wait until the snapshot is ready")
    args = parser.parse_args(argv)

    snapshot = ensure_snapshot(DaytonaSnapshotGateway(), args.name, args.image, build=args.build, wait=args.wait)
    print(snapshot.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
