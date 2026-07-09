from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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


def _control_command(env: dict[str, str]) -> list[str]:
    return [
        "uv",
        "run",
        "tomo-core",
        "control",
        "start",
        "--host",
        env.get("TOMO_CONTROL_HOST", "0.0.0.0"),
        "--port",
        env.get("TOMO_CONTROL_PORT", env.get("PORT", "8787")),
    ]


def _stop_children(processes: list[subprocess.Popen[object]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 10
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()

    for process in processes:
        if process.poll() is None:
            process.wait()


def start(env: dict[str, str]) -> int:
    commands = [_control_command(env)]
    if env.get("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"):
        commands.append(["uv", "run", "tomo-core", "telegram-shared", "start"])
        print("starting control api and shared telegram listener.", flush=True)
    else:
        print("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN is not set; starting control api only.", flush=True)

    processes = [subprocess.Popen(command, env=env, shell=False) for command in commands]
    shutdown_signal: int | None = None

    def forward_signal(signum: int, _frame: object) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signum
        _stop_children(processes)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)

    while shutdown_signal is None:
        for process in processes:
            code = process.poll()
            if code is not None:
                _stop_children(processes)
                return code if code != 0 else 0
        time.sleep(0.1)

    return 128 + shutdown_signal


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args != ["start"]:
        print("usage: railway_core_start.py [start]", file=sys.stderr)
        return 2
    return start(_base_env())


if __name__ == "__main__":
    raise SystemExit(main())
