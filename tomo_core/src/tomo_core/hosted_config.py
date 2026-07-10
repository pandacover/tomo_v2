from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_MAX_POLL_TIMEOUT = 60
_MAX_ROUTER_WORKERS = 32
_DAYTONA_HOSTED_VARIABLES = (
    "DAYTONA_API_KEY",
    "DAYTONA_API_URL",
    "DAYTONA_TARGET",
    "TOMO_DAYTONA_SNAPSHOT",
    "TOMO_DAYTONA_SANDBOX_DATA_DIR",
    "TOMO_SUPERGROK_OAUTH_JSON_B64",
)


@dataclass(frozen=True)
class HostedRuntimeConfig:
    runtime: str
    bot_token: str | None
    data_dir: Path
    daytona_api_key: str | None
    daytona_snapshot: str
    daytona_sandbox_data_dir: str
    oauth_json_b64: str | None
    poll_timeout: int
    worker_count: int

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        token: str | None = None,
        data_dir: str | None = None,
        static_response: str | None = None,
        poll_timeout: int | None = None,
        require_bot: bool = True,
    ) -> HostedRuntimeConfig:
        values = os.environ if env is None else env
        production_railway = values.get("RAILWAY_ENVIRONMENT_NAME") == "production" or values.get("RAILWAY_ENVIRONMENT") == "production"
        configured_runtime = values.get("TOMO_HOSTED_RUNTIME")
        if configured_runtime is None:
            runtime = "local" if static_response is not None else "daytona" if production_railway else None
        else:
            runtime = configured_runtime.lower()
        if runtime not in {"local", "daytona"}:
            raise ValueError("missing or invalid TOMO_HOSTED_RUNTIME (expected local or daytona)")
        if runtime == "local":
            configured_hosted_variables = [name for name in _DAYTONA_HOSTED_VARIABLES if name in values]
            if configured_hosted_variables:
                raise ValueError(f"local runtime cannot use Daytona hosted configuration: {', '.join(configured_hosted_variables)}")

        resolved_token = token if token is not None else values.get("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN")
        resolved_data_dir = data_dir if data_dir is not None else values.get("TOMO_CORE_DATA_DIR")
        missing = []
        if require_bot and not resolved_token:
            missing.append("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN")
        if not resolved_data_dir:
            missing.append("TOMO_CORE_DATA_DIR")
        if runtime == "daytona":
            for name in ("DAYTONA_API_KEY", "TOMO_DAYTONA_SNAPSHOT", "TOMO_DAYTONA_SANDBOX_DATA_DIR", "TOMO_SUPERGROK_OAUTH_JSON_B64"):
                if not values.get(name):
                    missing.append(name)
        if missing:
            raise ValueError(f"missing hosted runtime configuration: {', '.join(missing)}")

        if runtime == "daytona":
            sandbox_data_dir = values["TOMO_DAYTONA_SANDBOX_DATA_DIR"]
            if not _is_absolute_posix_path(sandbox_data_dir):
                raise ValueError("invalid TOMO_DAYTONA_SANDBOX_DATA_DIR")
            try:
                oauth_payload = json.loads(base64.b64decode(values["TOMO_SUPERGROK_OAUTH_JSON_B64"].encode("ascii"), validate=True))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("invalid TOMO_SUPERGROK_OAUTH_JSON_B64") from None
            if not isinstance(oauth_payload, dict):
                raise ValueError("invalid TOMO_SUPERGROK_OAUTH_JSON_B64")

        resolved_poll_timeout = poll_timeout if poll_timeout is not None else _parse_positive(
            values.get("TOMO_TELEGRAM_POLL_TIMEOUT", "30"), "TOMO_TELEGRAM_POLL_TIMEOUT", _MAX_POLL_TIMEOUT
        )
        if not 1 <= resolved_poll_timeout <= _MAX_POLL_TIMEOUT:
            raise ValueError("invalid TOMO_TELEGRAM_POLL_TIMEOUT")
        worker_count = _parse_positive(values.get("TOMO_ROUTER_WORKERS", "4"), "TOMO_ROUTER_WORKERS", _MAX_ROUTER_WORKERS)
        root = Path(resolved_data_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=root, prefix=".tomo-write-check-", delete=True):
                pass
        except OSError:
            raise ValueError("invalid or unwritable TOMO_CORE_DATA_DIR") from None

        return cls(
            runtime=runtime,
            bot_token=resolved_token,
            data_dir=root,
            daytona_api_key=values.get("DAYTONA_API_KEY") if runtime == "daytona" else None,
            daytona_snapshot=values.get("TOMO_DAYTONA_SNAPSHOT", ""),
            daytona_sandbox_data_dir=values.get("TOMO_DAYTONA_SANDBOX_DATA_DIR", ""),
            oauth_json_b64=values.get("TOMO_SUPERGROK_OAUTH_JSON_B64") if runtime == "daytona" else None,
            poll_timeout=resolved_poll_timeout,
            worker_count=worker_count,
        )


def _parse_positive(value: str, name: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {name}") from None
    if not 1 <= parsed <= maximum:
        raise ValueError(f"invalid {name}")
    return parsed


def _is_absolute_posix_path(value: str) -> bool:
    return value.startswith("/") and value.strip() == value
