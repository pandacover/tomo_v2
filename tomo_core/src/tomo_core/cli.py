from __future__ import annotations

import argparse
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from .instances import RuntimeInstanceRegistry
from .daytona_client import DaytonaClient
from .daytona_supervisor import DaytonaSupervisor
from .hosted_auth import HostedGrokAuth
from .models import OutboundBubble, RuntimeConfig
from .oauth import OAuthManager
from .grok_auth import GrokAuthStore
from .providers import GrokAuthProvider, OAuthBackedSuperGrokProvider, StaticProvider, XaiApiProvider, supergrok_oauth_provider_from_access_token
from .runtime import PersonalAgentRuntime
from .sandbox_inbound import SandboxInboundError, emit_failure, run_once
from .telegram import TelegramDeliverySink
from .telegram_bot import TelegramBotApiClient, TelegramPollingBot
from .onboarding_store import TelegramOnboardingStore
from .telegram_router import TelegramUpdateRouter
from .shared_gateway import SharedTelegramGateway
from .shared_gateway import HostedTelegramRuntimeDispatch
from .sandbox_dispatch import SandboxDispatch
from .sandbox_registry import SandboxRegistry
from .sandbox_protocol import RESULT_MARKER, decode_inbound, encode_result


def build_provider(args: argparse.Namespace, oauth: OAuthManager):
    if args.static_response:
        return StaticProvider(args.static_response)
    api_key = (
        getattr(args, "xai_api_key", None)
        or os.getenv("XAI_API_KEY")
        or os.getenv("TOMO_XAI_API_KEY")
        or getattr(args, "xai_access_token", None)  # deprecated alias
        or os.getenv("XAI_OAUTH_ACCESS_TOKEN")  # deprecated alias
        or os.getenv("TOMO_XAI_OAUTH_ACCESS_TOKEN")  # deprecated alias
    )
    if api_key:
        return XaiApiProvider(api_key=api_key, model=args.model)
    if getattr(args, "use_grok_login", False) or os.getenv("TOMO_USE_GROK_LOGIN") == "1":
        return GrokAuthProvider(auth_store=GrokAuthStore(), model=args.model)
    return OAuthBackedSuperGrokProvider(oauth=oauth, model=args.model)


def build_oauth_manager(args: argparse.Namespace) -> OAuthManager:
    providers = OAuthManager.default_providers(
        google_client_id=getattr(args, "google_client_id", None)
        or os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        or os.getenv("TOMO_GOOGLE_OAUTH_CLIENT_ID"),
        google_client_secret=getattr(args, "google_client_secret", None)
        or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        or os.getenv("TOMO_GOOGLE_OAUTH_CLIENT_SECRET"),
        supergrok_client_id=getattr(args, "supergrok_client_id", None)
        or os.getenv("SUPERGROK_OAUTH_CLIENT_ID")
        or os.getenv("GROK_OAUTH_CLIENT_ID")
        or os.getenv("TOMO_SUPERGROK_OAUTH_CLIENT_ID")
        or getattr(args, "xai_client_id", None),
        supergrok_client_secret=getattr(args, "supergrok_client_secret", None)
        or os.getenv("SUPERGROK_OAUTH_CLIENT_SECRET")
        or os.getenv("GROK_OAUTH_CLIENT_SECRET")
        or os.getenv("TOMO_SUPERGROK_OAUTH_CLIENT_SECRET")
        or getattr(args, "xai_client_secret", None),
        supergrok_auth_url=os.getenv("SUPERGROK_OAUTH_AUTH_URL", os.getenv("XAI_OAUTH_AUTH_URL", "https://auth.x.ai/oauth2/authorize")),
        supergrok_token_url=os.getenv("SUPERGROK_OAUTH_TOKEN_URL", os.getenv("XAI_OAUTH_TOKEN_URL", "https://auth.x.ai/oauth2/token")),
    )
    return OAuthManager(data_dir=args.data_dir, providers=providers)


def _shared_pid_path(data_dir: str) -> Path:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "telegram_shared.pid"


def _shared_log_path(data_dir: str) -> Path:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / "telegram_shared.log"


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def stop_shared_gateway(data_dir: str, timeout_seconds: float = 10) -> int:
    pid_path = _shared_pid_path(data_dir)
    pid = _read_pid(pid_path)
    if pid is None:
        pid_path.unlink(missing_ok=True)
        print("shared telegram gateway is not running.")
        return 0
    if not _pid_is_running(pid):
        pid_path.unlink(missing_ok=True)
        print("shared telegram gateway pid was stale; cleaned it up.")
        return 0
    print(f"stopping shared telegram gateway pid {pid}.")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_is_running(pid):
            pid_path.unlink(missing_ok=True)
            return 0
        time.sleep(0.2)
    if hasattr(signal, "SIGKILL"):
        os.kill(pid, signal.SIGKILL)
    pid_path.unlink(missing_ok=True)
    return 0


def _append_arg(argv: list[str], name: str, value: object | None) -> None:
    if value is not None:
        argv.extend([name, str(value)])


def start_shared_gateway_background(args: argparse.Namespace) -> int:
    missing = _missing_shared_gateway_configuration(args)
    if missing:
        print(f"missing shared hosted gateway configuration: {', '.join(missing)}", file=sys.stderr)
        return 2
    pid_path = _shared_pid_path(args.data_dir)
    existing_pid = _read_pid(pid_path)
    if existing_pid is not None and _pid_is_running(existing_pid):
        print(f"shared telegram gateway already running as pid {existing_pid}.")
        return 0

    child_argv = [sys.executable, "-m", "tomo_core.cli", "telegram-shared", "start"]
    _append_arg(child_argv, "--token", args.token)
    _append_arg(child_argv, "--bot-username", args.bot_username)
    _append_arg(child_argv, "--data-dir", args.data_dir)
    _append_arg(child_argv, "--soul", args.soul)
    _append_arg(child_argv, "--model", args.model)
    _append_arg(child_argv, "--static-response", args.static_response)
    _append_arg(child_argv, "--poll-timeout", args.poll_timeout)

    log_path = _shared_log_path(args.data_dir)
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "cwd": os.getcwd(),
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    with log_path.open("ab") as log:
        process = subprocess.Popen(child_argv, stdout=log, stderr=subprocess.STDOUT, **popen_kwargs)
    pid_path.write_text(str(process.pid), encoding="utf-8")
    print(f"shared telegram gateway started in background as pid {process.pid}; logs: {log_path}")
    return 0


def run_shared_gateway_foreground(args: argparse.Namespace) -> int:
    missing = _missing_shared_gateway_configuration(args)
    if missing:
        print(f"missing shared hosted gateway configuration: {', '.join(missing)}", file=sys.stderr)
        return 2
    pid_path = _shared_pid_path(args.data_dir)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        client = TelegramBotApiClient(token=args.token)
        store = TelegramOnboardingStore(args.data_dir)
        if args.static_response:
            instances = RuntimeInstanceRegistry(args.data_dir, lambda _: StaticProvider(args.static_response), client, soul_path=args.soul)
            gateway = SharedTelegramGateway(client=client, store=store, instances=instances)
        else:
            auth = HostedGrokAuth.from_environment(auth_path=Path(args.data_dir) / "supergrok_auth.json")
            auth.bootstrap()
            registry = SandboxRegistry(args.data_dir)
            daytona = DaytonaClient()
            supervisor = DaytonaSupervisor(registry, daytona, snapshot=os.environ["TOMO_DAYTONA_SNAPSHOT_NAME"])
            dispatch = HostedTelegramRuntimeDispatch(client, supervisor, SandboxDispatch(registry, daytona, auth.access_token).dispatch)
            gateway = SharedTelegramGateway(client=client, store=store, dispatch=dispatch)
        print("shared telegram gateway polling started. press ctrl+c to stop.")
        TelegramUpdateRouter(
            client=client,
            store=store,
            process_update=gateway.process_update,
            poll_timeout=args.poll_timeout,
        ).run_forever()
    finally:
        if _read_pid(pid_path) == os.getpid():
            pid_path.unlink(missing_ok=True)


def _missing_shared_gateway_configuration(args: argparse.Namespace) -> list[str]:
    if args.static_response:
        return [] if args.token else ["TOMO_TELEGRAM_GLOBAL_BOT_TOKEN or --token"]
    required = {
        "DAYTONA_API_KEY": os.getenv("DAYTONA_API_KEY"),
        "TOMO_DAYTONA_SNAPSHOT_NAME": os.getenv("TOMO_DAYTONA_SNAPSHOT_NAME"),
        "TOMO_SUPERGROK_OAUTH_JSON_B64": os.getenv("TOMO_SUPERGROK_OAUTH_JSON_B64"),
        "TOMO_TELEGRAM_GLOBAL_BOT_TOKEN or --token": args.token,
    }
    return [name for name, value in required.items() if not value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tomo-core", description="run the tomo core telegram bot")
    sub = parser.add_subparsers(dest="command", required=True)

    telegram = sub.add_parser("telegram", help="telegram bot commands")
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    start = telegram_sub.add_parser("start", help="start telegram long polling")
    start.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOMO_TELEGRAM_BOT_TOKEN"))
    start.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    start.add_argument("--soul", default=os.getenv("TOMO_CORE_SOUL", "SOUL.md"))
    start.add_argument("--model", default=os.getenv("TOMO_XAI_MODEL", "grok-composer-2.5-fast"))
    start.add_argument("--xai-api-key", default=None)
    start.add_argument("--use-grok-login", action="store_true", help="use ~/.grok/auth.json from `grok login`")
    start.add_argument("--supergrok-client-id", default=None)
    start.add_argument("--supergrok-client-secret", default=None)
    start.add_argument("--xai-access-token", default=None, help="deprecated alias for --xai-api-key")
    start.add_argument("--xai-client-id", default=None, help="deprecated alias for --supergrok-client-id")
    start.add_argument("--xai-client-secret", default=None, help="deprecated alias for --supergrok-client-secret")
    start.add_argument("--google-client-id", default=None)
    start.add_argument("--google-client-secret", default=None)
    start.add_argument("--static-response", default=os.getenv("TOMO_CORE_STATIC_RESPONSE"))
    start.add_argument("--poll-timeout", type=int, default=30)

    control = sub.add_parser("control", help="control api commands")
    control_sub = control.add_subparsers(dest="control_command", required=True)
    control_start = control_sub.add_parser("start", help="start control api")
    control_start.add_argument("--host", default=os.getenv("TOMO_CONTROL_HOST", "127.0.0.1"))
    control_start.add_argument("--port", type=int, default=int(os.getenv("TOMO_CONTROL_PORT", "8787")))
    control_start.add_argument("--reload", action="store_true")

    shared = sub.add_parser("telegram-shared", help="shared hosted telegram gateway")
    shared_sub = shared.add_subparsers(dest="shared_command", required=True)
    shared_start = shared_sub.add_parser("start", help="start shared telegram polling")
    shared_start.add_argument("--token", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"))
    shared_start.add_argument("--bot-username", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME"))
    shared_start.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    shared_start.add_argument("--soul", default=os.getenv("TOMO_CORE_SOUL", "SOUL.md"))
    shared_start.add_argument("--model", default=os.getenv("TOMO_XAI_MODEL", "grok-composer-2.5-fast"))
    shared_start.add_argument("--xai-api-key", default=None)
    shared_start.add_argument("--use-grok-login", action="store_true", help="use ~/.grok/auth.json from `grok login`")
    shared_start.add_argument("--supergrok-client-id", default=None)
    shared_start.add_argument("--supergrok-client-secret", default=None)
    shared_start.add_argument("--google-client-id", default=None)
    shared_start.add_argument("--google-client-secret", default=None)
    shared_start.add_argument("--static-response")
    shared_start.add_argument("--poll-timeout", type=int, default=30)
    shared_start.add_argument("--background", action="store_true", help="start poller in the background and return")

    shared_stop = shared_sub.add_parser("stop", help="stop background shared telegram polling")
    shared_stop.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))

    shared_restart = shared_sub.add_parser("restart", help="restart shared telegram polling in the background")
    shared_restart.add_argument("--token", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"))
    shared_restart.add_argument("--bot-username", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME"))
    shared_restart.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    shared_restart.add_argument("--soul", default=os.getenv("TOMO_CORE_SOUL", "SOUL.md"))
    shared_restart.add_argument("--model", default=os.getenv("TOMO_XAI_MODEL", "grok-composer-2.5-fast"))
    shared_restart.add_argument("--xai-api-key", default=None)
    shared_restart.add_argument("--use-grok-login", action="store_true", help="use ~/.grok/auth.json from `grok login`")
    shared_restart.add_argument("--supergrok-client-id", default=None)
    shared_restart.add_argument("--supergrok-client-secret", default=None)
    shared_restart.add_argument("--google-client-id", default=None)
    shared_restart.add_argument("--google-client-secret", default=None)
    shared_restart.add_argument("--static-response")
    shared_restart.add_argument("--poll-timeout", type=int, default=30)

    sandbox_inbound = sub.add_parser("sandbox-inbound", help="handle one sandbox protocol envelope from TOMO_INBOUND_JSON")
    sandbox_inbound.add_argument("--health", action="store_true", help="validate the sandbox boundary without calling a model")

    args = parser.parse_args(argv)
    if args.command == "telegram" and args.telegram_command == "start":
        if not args.token:
            print("missing telegram bot token. set TELEGRAM_BOT_TOKEN or pass --token.", file=sys.stderr)
            return 2
        client = TelegramBotApiClient(token=args.token)
        oauth = build_oauth_manager(args)
        provider = build_provider(args, oauth)
        runtime = PersonalAgentRuntime(
            provider=provider,
            telegram=TelegramDeliverySink(client),
            config=RuntimeConfig(data_dir=args.data_dir, soul_path=args.soul),
        )
        print("telegram bot polling started. press ctrl+c to stop.")
        TelegramPollingBot(client=client, runtime=runtime, oauth=oauth, poll_timeout=args.poll_timeout).run_forever()

    if args.command == "control" and args.control_command == "start":
        uvicorn.run("tomo_core.control_api:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "telegram-shared" and args.shared_command == "start":
        if args.background:
            return start_shared_gateway_background(args)
        return run_shared_gateway_foreground(args)

    if args.command == "telegram-shared" and args.shared_command == "stop":
        return stop_shared_gateway(args.data_dir)

    if args.command == "telegram-shared" and args.shared_command == "restart":
        stop_shared_gateway(args.data_dir)
        return start_shared_gateway_background(args)

    if args.command == "sandbox-inbound":
        if args.health:
            sys.stdout.write(f"{RESULT_MARKER}{encode_result('health-1', [OutboundBubble('healthy')])}\n")
            sys.stdout.flush()
            return 0
        payload = os.getenv("TOMO_INBOUND_JSON")
        if payload is None:
            emit_failure(sys.stdout, "missing_inbound")
            return 1
        access_token = os.getenv("TOMO_SUPERGROK_ACCESS_TOKEN")
        if not access_token:
            emit_failure(sys.stdout, "missing_access_token")
            return 1
        try:
            provider = supergrok_oauth_provider_from_access_token(access_token)
            return run_once(
                io.StringIO(payload),
                sys.stdout,
                data_dir=os.getenv("TOMO_CORE_DATA_DIR", "/home/daytona/.tomo"),
                provider=provider,
                secret_values=(access_token,),
            )
        except SandboxInboundError:
            return 1
        except Exception:
            emit_failure(sys.stdout, "provider_failed")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
