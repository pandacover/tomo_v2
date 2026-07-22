from __future__ import annotations

import argparse
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import uvicorn

from .instances import RuntimeInstanceRegistry
from .daytona_client import DaytonaClient
from .daytona_supervisor import DaytonaSupervisor
from .hosted_auth import HostedSuperGrokTokenBroker
from .hosted_config import HostedRuntimeConfig
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
from .shared_gateway import InProcessTelegramRuntimeDispatch, SharedTelegramGateway
from .peer_exchange import PeerExchange
from .peer_service import PeerService
from .sandbox_dispatch import SandboxDispatch
from .sandbox_registry import SandboxRegistry
from .sandbox_protocol import RESULT_MARKER, decode_inbound, encode_result
from .cron_service import CronSchedulerService
from .cron_store import CronStore
from .cron_capability import load_or_create_key
from .peer_capability import load_or_create_key as load_or_create_peer_key
from .attachment_capability import load_or_create_attachment_key
from .attachment_reader import ControlAttachmentReader
from .vision import ProviderVisionInterpreter
from .personal_data_transfer import export_owner, import_owner
from .sqlite_personal_data import SqlitePersonalDataRepository


def _log_shared_gateway_error(error: Exception) -> None:
    raw_code = getattr(error, "error_code", "processing_error")
    code = re.sub(r"[^a-z0-9]+", "_", str(raw_code).lower()).strip("_")[:64] or "processing_error"
    exception_class = re.sub(r"[^A-Za-z0-9_]+", "_", type(error).__name__)[:64] or "Exception"
    print(
        f"telegram worker failure code={code} exception_class={exception_class}",
        file=sys.stderr,
        flush=True,
    )


def _cron_job_payload(job) -> dict:
    return {
        "job_id": job.job_id,
        "owner_id": job.owner_id,
        "intent": {"text": job.intent.text, "constraints": list(job.intent.constraints)},
        "schedule": {
            "kind": job.schedule.kind,
            "at": None if job.schedule.at is None else job.schedule.at.isoformat(),
            "every_seconds": None if job.schedule.every is None else job.schedule.every.total_seconds(),
            "expression": job.schedule.expression,
            "timezone_name": job.schedule.timezone_name,
            "starts_at": None if job.schedule.starts_at is None else job.schedule.starts_at.isoformat(),
        },
        "lifecycle": {
            "ends_at": None if job.lifecycle.ends_at is None else job.lifecycle.ends_at.isoformat(),
            "max_successful_runs": job.lifecycle.max_successful_runs,
        },
        "destination": job.destination,
        "status": job.status,
        "revision": job.revision,
    }


def _cron_run_payload(run) -> dict:
    return {
        "run_id": run.run_id,
        "job_id": run.job_id,
        "status": run.status,
        "trigger": run.trigger,
        "scheduled_for": run.scheduled_for.isoformat(),
        "revision": run.revision,
    }


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


def build_vision_interpreter(
    args: argparse.Namespace,
    oauth: OAuthManager,
    reader: TelegramBotApiClient,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
):
    """Create a role-specific provider while reusing the selected local auth source."""
    if args.static_response:
        return None
    vision_model = model or os.getenv("TOMO_XAI_VISION_MODEL", "grok-4.3")
    vision_effort = reasoning_effort or os.getenv("TOMO_XAI_VISION_REASONING_EFFORT", "low")
    base = build_provider(args, oauth)
    if isinstance(base, XaiApiProvider):
        provider = XaiApiProvider(base.api_key, model=vision_model, base_url=base.base_url, reasoning_effort=vision_effort, store=False)
    elif isinstance(base, GrokAuthProvider):
        provider = GrokAuthProvider(base.auth_store, model=vision_model, base_url=base.base_url, reasoning_effort=vision_effort, store=False)
    elif isinstance(base, OAuthBackedSuperGrokProvider):
        provider = OAuthBackedSuperGrokProvider(base.oauth, model=vision_model, base_url=base.base_url, reasoning_effort=vision_effort, store=False)
    else:
        return None
    return ProviderVisionInterpreter(provider, reader)


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


def start_shared_gateway_background(args: argparse.Namespace, config: HostedRuntimeConfig) -> int:
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
    _append_arg(child_argv, "--poll-timeout", config.poll_timeout)

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


def run_shared_gateway_foreground(args: argparse.Namespace, config: HostedRuntimeConfig) -> int:
    pid_path = _shared_pid_path(args.data_dir)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        client = TelegramBotApiClient(token=config.bot_token)
        store = TelegramOnboardingStore(config.data_dir, input_debounce_seconds=config.telegram_input_debounce_seconds)
        capability_key = load_or_create_key(config.data_dir)
        peer_capability_key = load_or_create_peer_key(config.data_dir)
        cron_store = CronStore(config.data_dir)
        peer_exchange = PeerExchange(
            config.data_dir,
            source_generation_active=store.generation_not_superseded,
            source_generation_guard=store.generation_guard,
        )
        if config.runtime == "local":
            oauth = build_oauth_manager(args)
            provider_factory = lambda _: StaticProvider(args.static_response) if args.static_response else build_provider(args, oauth)
            instances = RuntimeInstanceRegistry(
                config.data_dir,
                provider_factory,
                client,
                soul_path=args.soul,
                vision_interpreter_factory=lambda _: build_vision_interpreter(
                    args,
                    oauth,
                    client,
                    model=config.xai_vision_model,
                    reasoning_effort=config.xai_vision_reasoning_effort,
                ),
            )
            gateway = SharedTelegramGateway(
                client=client,
                store=store,
                dispatch=InProcessTelegramRuntimeDispatch(
                    instances,
                    control_url=config.control_public_url,
                    capability_key=capability_key,
                    peer_capability_key=peer_capability_key,
                ),
                pace_seconds=config.telegram_delivery_pace_seconds,
                cron_store=cron_store,
                peer_exchange=peer_exchange,
            )
        else:
            attachment_capability_key = load_or_create_attachment_key(config.data_dir)
            auth = HostedSuperGrokTokenBroker(config.data_dir, os.getenv("TOMO_SUPERGROK_OAUTH_JSON_B64"))
            auth.access_token()
            registry = SandboxRegistry(config.data_dir)
            daytona = DaytonaClient()
            supervisor = DaytonaSupervisor(
                registry,
                daytona,
                auth,
                snapshot=config.daytona_snapshot,
                data_dir=config.daytona_sandbox_data_dir,
                xai_model=config.xai_model,
                xai_reasoning_effort=config.xai_reasoning_effort,
            )
            dispatch = SandboxDispatch(
                supervisor,
                daytona,
                auth,
                data_dir=config.daytona_sandbox_data_dir,
                xai_model=config.xai_model,
                xai_reasoning_effort=config.xai_reasoning_effort,
                xai_vision_model=config.xai_vision_model,
                xai_vision_reasoning_effort=config.xai_vision_reasoning_effort,
                control_url=config.control_public_url,
                capability_key=capability_key,
                peer_capability_key=peer_capability_key,
                attachment_capability_key=attachment_capability_key,
            )
            gateway = SharedTelegramGateway(
                client=client,
                store=store,
                dispatch=dispatch,
                pace_seconds=config.telegram_delivery_pace_seconds,
                cron_store=cron_store,
                peer_exchange=peer_exchange,
            )
        cron_service = CronSchedulerService(
            cron_store,
            gateway.execute_cron_claim,
            gateway.send_cron_delivery,
            delivery_pace_seconds=config.telegram_delivery_pace_seconds,
        )
        peer_service = PeerService(peer_exchange, store, gateway.dispatch, send_notice=gateway.send_peer_notice)
        print("shared telegram gateway polling started. press ctrl+c to stop.")

        def stop_on_sigterm(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        previous_sigterm = signal.signal(signal.SIGTERM, stop_on_sigterm)
        try:
            cron_service.start()
            peer_service.start()
            TelegramUpdateRouter(
                client=client,
                store=store,
                process_update=gateway.process_update,
                cancel_generation=getattr(gateway.dispatch, "cancel_generation", None),
                poll_timeout=config.poll_timeout,
                worker_count=config.worker_count,
                on_error=_log_shared_gateway_error,
            ).run_forever()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
    finally:
        if "cron_service" in locals():
            cron_service.stop()
        if "peer_service" in locals():
            peer_service.stop()
        if _read_pid(pid_path) == os.getpid():
            pid_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tomo-core", description="run the tomo core telegram bot")
    sub = parser.add_subparsers(dest="command", required=True)

    telegram = sub.add_parser("telegram", help="telegram bot commands")
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    start = telegram_sub.add_parser("start", help="start telegram long polling")
    start.add_argument("--token", default=os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOMO_TELEGRAM_BOT_TOKEN"))
    start.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    start.add_argument("--soul", default=os.getenv("TOMO_CORE_SOUL", "SOUL.md"))
    start.add_argument("--model", default=os.getenv("TOMO_XAI_MODEL", "grok-4.5"))
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

    cron = sub.add_parser("cron", help="operator cron inspection commands")
    cron_sub = cron.add_subparsers(dest="cron_command", required=True)
    for name, help_text in (("inspect", "inspect one owner-scoped cron job"), ("run-once", "request one immediate owner-scoped run")):
        command = cron_sub.add_parser(name, help=help_text)
        command.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
        command.add_argument("--owner", required=True)
        command.add_argument("--job-id", required=True)
    cron_sub.choices["run-once"].add_argument("--expected-revision", type=int)

    shared = sub.add_parser("telegram-shared", help="shared hosted telegram gateway")
    shared_sub = shared.add_subparsers(dest="shared_command", required=True)
    shared_start = shared_sub.add_parser("start", help="start shared telegram polling")
    shared_start.add_argument("--token", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"))
    shared_start.add_argument("--bot-username", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME"))
    shared_start.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    shared_start.add_argument("--soul", default=os.getenv("TOMO_CORE_SOUL", "SOUL.md"))
    shared_start.add_argument("--model", default=os.getenv("TOMO_XAI_MODEL", "grok-4.5"))
    shared_start.add_argument("--xai-api-key", default=None)
    shared_start.add_argument("--use-grok-login", action="store_true", help="use ~/.grok/auth.json from `grok login`")
    shared_start.add_argument("--supergrok-client-id", default=None)
    shared_start.add_argument("--supergrok-client-secret", default=None)
    shared_start.add_argument("--google-client-id", default=None)
    shared_start.add_argument("--google-client-secret", default=None)
    shared_start.add_argument("--static-response")
    shared_start.add_argument("--poll-timeout", type=int)
    shared_start.add_argument("--background", action="store_true", help="start poller in the background and return")

    shared_stop = shared_sub.add_parser("stop", help="stop background shared telegram polling")
    shared_stop.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))

    shared_restart = shared_sub.add_parser("restart", help="restart shared telegram polling in the background")
    shared_restart.add_argument("--token", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN"))
    shared_restart.add_argument("--bot-username", default=os.getenv("TOMO_TELEGRAM_GLOBAL_BOT_USERNAME"))
    shared_restart.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
    shared_restart.add_argument("--soul", default=os.getenv("TOMO_CORE_SOUL", "SOUL.md"))
    shared_restart.add_argument("--model", default=os.getenv("TOMO_XAI_MODEL", "grok-4.5"))
    shared_restart.add_argument("--xai-api-key", default=None)
    shared_restart.add_argument("--use-grok-login", action="store_true", help="use ~/.grok/auth.json from `grok login`")
    shared_restart.add_argument("--supergrok-client-id", default=None)
    shared_restart.add_argument("--supergrok-client-secret", default=None)
    shared_restart.add_argument("--google-client-id", default=None)
    shared_restart.add_argument("--google-client-secret", default=None)
    shared_restart.add_argument("--static-response")
    shared_restart.add_argument("--poll-timeout", type=int)

    sandbox_inbound = sub.add_parser("sandbox-inbound", help="handle one sandbox protocol envelope from TOMO_INBOUND_JSON")
    sandbox_inbound.add_argument("--health", action="store_true", help="validate the sandbox boundary without calling a model")

    personal_data = sub.add_parser("personal-data", help="personal-data maintenance commands")
    personal_data_sub = personal_data.add_subparsers(dest="personal_data_command", required=True)
    for name, help_text in (("rebuild-index", "rebuild disposable search indexes"), ("integrity-check", "check SQLite integrity")):
        command = personal_data_sub.add_parser(name, help=help_text)
        command.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core"))
        command.add_argument("--owner")
    export = personal_data_sub.add_parser("export", help="export one owner's canonical JSONL")
    export.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core")); export.add_argument("--owner", required=True); export.add_argument("--output", required=True)
    import_data = personal_data_sub.add_parser("import", help="import one owner's canonical JSONL; peer records remain inert")
    import_data.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core")); import_data.add_argument("--owner", required=True); import_data.add_argument("--input", required=True)
    delete_owner = personal_data_sub.add_parser("delete-owner", help="permanently delete one owner's data")
    delete_owner.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core")); delete_owner.add_argument("--owner", required=True); delete_owner.add_argument("--confirm", action="store_true")
    settings = personal_data_sub.add_parser("settings", help="inspect or update owner memory settings")
    settings.add_argument("--data-dir", default=os.getenv("TOMO_CORE_DATA_DIR", ".tomo_core")); settings.add_argument("--owner", required=True)
    settings.add_argument("--capture-enabled", choices=("true", "false")); settings.add_argument("--retrieval-enabled", choices=("true", "false")); settings.add_argument("--reactions-enabled", choices=("true", "false"))

    args = parser.parse_args(argv)
    if args.command == "cron":
        store = CronStore(args.data_dir)
        if args.cron_command == "inspect":
            job = store.get(args.owner, args.job_id)
            if job is None:
                print("cron job not found", file=sys.stderr)
                return 1
            print(json.dumps(_cron_job_payload(job), ensure_ascii=True, separators=(",", ":")))
            return 0
        run = store.request_manual_run(args.owner, args.job_id, args.expected_revision)
        if run is None:
            print("cron run not requested", file=sys.stderr)
            return 1
        print(json.dumps(_cron_run_payload(run), ensure_ascii=True, separators=(",", ":")))
        return 0
    if args.command == "personal-data":
        repository = SqlitePersonalDataRepository(Path(args.data_dir) / "tomo.sqlite3")
        if args.personal_data_command == "rebuild-index":
            repository.rebuild_index(args.owner); print("search indexes rebuilt"); return 0
        if args.personal_data_command == "integrity-check":
            healthy = repository.integrity_check(); print("ok" if healthy else "failed"); return 0 if healthy else 1
        if args.personal_data_command == "export":
            peer_records = PeerExchange(args.data_dir).export_owner_records(
                owner_id=args.owner
            )
            with Path(args.output).open("w", encoding="utf-8") as output:
                export_owner(repository, args.owner, output, peer_records=peer_records)
            return 0
        if args.personal_data_command == "import":
            with Path(args.input).open(encoding="utf-8") as source:
                import_owner(repository, args.owner, source, peer_exchange=PeerExchange(args.data_dir))
            return 0
        if args.personal_data_command == "delete-owner":
            if not args.confirm:
                print("refusing owner deletion without --confirm", file=sys.stderr); return 2
            PeerExchange(args.data_dir).delete_owner(args.owner)
            TelegramOnboardingStore(args.data_dir).delete_owner(args.owner)
            repository.delete_owner(args.owner); return 0
        for setting in ("capture_enabled", "retrieval_enabled", "reactions_enabled"):
            value = getattr(args, setting)
            if value is not None: repository.update_memory_setting(args.owner, setting, value == "true")
        print(repository.memory_settings(args.owner)); return 0
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
            vision_interpreter=build_vision_interpreter(args, oauth, client),
        )
        print("telegram bot polling started. press ctrl+c to stop.")
        TelegramPollingBot(client=client, runtime=runtime, oauth=oauth, poll_timeout=args.poll_timeout).run_forever()

    if args.command == "control" and args.control_command == "start":
        uvicorn.run("tomo_core.control_api:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    if args.command == "telegram-shared" and args.shared_command == "start":
        try:
            config = HostedRuntimeConfig.from_env(token=args.token, data_dir=args.data_dir, static_response=args.static_response, poll_timeout=args.poll_timeout)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        if args.background:
            return start_shared_gateway_background(args, config)
        return run_shared_gateway_foreground(args, config)

    if args.command == "telegram-shared" and args.shared_command == "stop":
        return stop_shared_gateway(args.data_dir)

    if args.command == "telegram-shared" and args.shared_command == "restart":
        try:
            config = HostedRuntimeConfig.from_env(token=args.token, data_dir=args.data_dir, static_response=args.static_response, poll_timeout=args.poll_timeout)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        stop_shared_gateway(args.data_dir)
        return start_shared_gateway_background(args, config)

    if args.command == "sandbox-inbound":
        if args.health:
            sys.stdout.write(f"{RESULT_MARKER}{encode_result('health-1', [OutboundBubble('healthy')])}\n")
            sys.stdout.flush()
            return 0
        payload = os.getenv("TOMO_PEER_TURN_JSON") or os.getenv("TOMO_AUTOMATION_JSON") or os.getenv("TOMO_INBOUND_JSON")
        if payload is None:
            emit_failure(sys.stdout, "missing_inbound")
            return 1
        access_token = os.getenv("TOMO_SUPERGROK_ACCESS_TOKEN")
        if not access_token:
            emit_failure(sys.stdout, "missing_access_token")
            return 1
        data_dir = os.getenv("TOMO_CORE_DATA_DIR")
        if not data_dir:
            emit_failure(sys.stdout, "missing_data_dir")
            return 1
        owner_id = os.getenv("TOMO_INSTANCE_ID")
        if not owner_id:
            emit_failure(sys.stdout, "missing_owner_id")
            return 1
        try:
            provider = supergrok_oauth_provider_from_access_token(
                access_token,
                model=os.getenv("TOMO_XAI_MODEL", "grok-4.5"),
                reasoning_effort=os.getenv("TOMO_XAI_REASONING_EFFORT", "high"),
            )
            vision = ProviderVisionInterpreter(
                supergrok_oauth_provider_from_access_token(
                    access_token,
                    model=os.getenv("TOMO_XAI_VISION_MODEL", "grok-4.3"),
                    reasoning_effort=os.getenv("TOMO_XAI_VISION_REASONING_EFFORT", "low"),
                    store=False,
                ),
                ControlAttachmentReader.from_env(),
            ) if os.getenv("TOMO_ATTACHMENT_CAPABILITY") else None
            return run_once(
                io.StringIO(payload),
                sys.stdout,
                config=RuntimeConfig(
                    data_dir=data_dir,
                    soul_path=os.getenv("TOMO_CORE_SOUL", "SOUL.md"),
                    owner_id=owner_id,
                ),
                provider=provider,
                vision_interpreter=vision,
                secret_values=tuple(value for value in (access_token, os.getenv("TOMO_CRON_CAPABILITY"), os.getenv("TOMO_PEER_CAPABILITY"), os.getenv("TOMO_ATTACHMENT_CAPABILITY")) if value),
            )
        except SandboxInboundError:
            return 1
        except Exception:
            emit_failure(sys.stdout, "provider_failed")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
