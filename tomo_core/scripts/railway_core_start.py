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


def _start(name: str, args: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    print(f"starting {name}: {' '.join(args)}", flush=True)
    return subprocess.Popen(args, env=env, text=True)


def main() -> int:
    env = os.environ.copy()
    env.setdefault("TOMO_CORE_DATA_DIR", _default_data_dir())
    env.setdefault("TOMO_CONTROL_HOST", "0.0.0.0")
    env.setdefault("TOMO_CONTROL_PORT", env.get("PORT", "8787"))

    children: list[tuple[str, subprocess.Popen[str]]] = []

    def stop_children(*_: object) -> None:
        for name, proc in children:
            if proc.poll() is None:
                print(f"stopping {name}", flush=True)
                proc.terminate()
        deadline = time.time() + 10
        for name, proc in children:
            if proc.poll() is None:
                try:
                    proc.wait(max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    print(f"killing {name}", flush=True)
                    proc.kill()

    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)

    if env.get("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"):
        children.append(
            (
                "telegram-shared",
                _start("telegram-shared", ["uv", "run", "tomo-core", "telegram-shared", "start"], env),
            )
        )
    else:
        print("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN is not set; starting control api only.", flush=True)

    children.append(
        (
            "control-api",
            _start(
                "control-api",
                [
                    "uv",
                    "run",
                    "tomo-core",
                    "control",
                    "start",
                    "--host",
                    env["TOMO_CONTROL_HOST"],
                    "--port",
                    env["TOMO_CONTROL_PORT"],
                ],
                env,
            ),
        )
    )

    try:
        while True:
            for name, proc in children:
                code = proc.poll()
                if code is not None:
                    print(f"{name} exited with code {code}", flush=True)
                    stop_children()
                    return int(code)
            time.sleep(1)
    finally:
        stop_children()


if __name__ == "__main__":
    raise SystemExit(main())
