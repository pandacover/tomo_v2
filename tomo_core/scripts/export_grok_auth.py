from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from tomo_core.grok_auth import default_grok_auth_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="encode Grok OAuth JSON for TOMO_GROK_AUTH_B64")
    parser.add_argument("auth_path", nargs="?", type=Path, default=default_grok_auth_path())
    args = parser.parse_args(argv)
    try:
        raw = args.auth_path.read_bytes()
        if not isinstance(json.loads(raw), dict):
            raise ValueError
    except (OSError, ValueError, json.JSONDecodeError):
        print("could not read a valid Grok authentication file", file=sys.stderr)
        return 1
    sys.stdout.write(base64.b64encode(raw).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
