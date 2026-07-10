from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from tomo_core.grok_auth import default_grok_auth_path


def normalize_auth_path(path: Path) -> Path:
    """Translate a Git Bash `/c/...` argument for native Windows Python."""
    if os.name != "nt" or path.drive:
        return path
    parts = path.parts
    if path.root and len(parts) >= 2 and len(parts[1]) == 1 and parts[1].isalpha():
        return Path(f"{parts[1].upper()}:/", *parts[2:])
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="encode Grok OAuth JSON for TOMO_SUPERGROK_OAUTH_JSON_B64")
    parser.add_argument("auth_path", nargs="?", type=Path, default=default_grok_auth_path())
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    auth_path = normalize_auth_path(args.auth_path)
    try:
        raw = auth_path.read_bytes()
        if not isinstance(json.loads(raw), dict):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        print("could not read a valid Grok authentication file", file=sys.stderr)
        return 1
    sys.stdout.write(base64.b64encode(raw).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
