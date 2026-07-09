from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _default_data_dir() -> str:
    if os.getenv("TOMO_CORE_DATA_DIR"):
        return os.environ["TOMO_CORE_DATA_DIR"]
    if os.getenv("TOMO_DATA_DIR"):
        return os.environ["TOMO_DATA_DIR"]
    if Path("/data").exists():
        return "/data"
    return ".tomo_core"


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("TOMO_CORE_DATA_DIR", _default_data_dir())
    env.setdefault("TOMO_CONTROL_HOST", "0.0.0.0")
    env.setdefault("TOMO_CONTROL_PORT", env.get("PORT", "8787"))
    return env


def _run(args: list[str], env: dict[str, str]) -> int:
    print(f"running: {' '.join(args)}", flush=True)
    return subprocess.run(args, env=env, check=False).returncode


def _run_control(env: dict[str, str]) -> int:
    args = [
        "uv",
        "run",
        "tomo-core",
        "control",
        "start",
        "--host",
        env["TOMO_CONTROL_HOST"],
        "--port",
        env["TOMO_CONTROL_PORT"],
    ]
    return _run(args, env)


def start(env: dict[str, str]) -> int:
    if env.get("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"):
        code = _run(["uv", "run", "tomo-core", "telegram-shared", "restart"], env)
        if code != 0:
            return code
    else:
        print("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN is not set; starting control api only.", flush=True)
    return _run_control(env)


def stop(env: dict[str, str]) -> int:
    return _run(["uv", "run", "tomo-core", "telegram-shared", "stop"], env)


def restart(env: dict[str, str]) -> int:
    stop_code = stop(env)
    if stop_code != 0:
        return stop_code
    return start(env)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    command = args[0] if args else "start"
    env = _base_env()
    if command == "start":
        return start(env)
    if command == "stop":
        return stop(env)
    if command == "restart":
        return restart(env)
    print("usage: railway_core_start.py [start|stop|restart]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
