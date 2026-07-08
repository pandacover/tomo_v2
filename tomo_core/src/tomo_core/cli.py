from __future__ import annotations

import argparse
import os
import sys

from .models import RuntimeConfig
from .oauth import OAuthManager
from .grok_auth import GrokAuthStore
from .providers import GrokAuthProvider, OAuthBackedSuperGrokProvider, StaticProvider, XaiApiProvider
from .runtime import PersonalAgentRuntime
from .telegram import TelegramDeliverySink
from .telegram_bot import TelegramBotApiClient, TelegramPollingBot


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
        google_client_id=args.google_client_id or os.getenv("GOOGLE_OAUTH_CLIENT_ID") or os.getenv("TOMO_GOOGLE_OAUTH_CLIENT_ID"),
        google_client_secret=args.google_client_secret
        or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        or os.getenv("TOMO_GOOGLE_OAUTH_CLIENT_SECRET"),
        supergrok_client_id=args.supergrok_client_id
        or os.getenv("SUPERGROK_OAUTH_CLIENT_ID")
        or os.getenv("GROK_OAUTH_CLIENT_ID")
        or os.getenv("TOMO_SUPERGROK_OAUTH_CLIENT_ID")
        or getattr(args, "xai_client_id", None),
        supergrok_client_secret=args.supergrok_client_secret
        or os.getenv("SUPERGROK_OAUTH_CLIENT_SECRET")
        or os.getenv("GROK_OAUTH_CLIENT_SECRET")
        or os.getenv("TOMO_SUPERGROK_OAUTH_CLIENT_SECRET")
        or getattr(args, "xai_client_secret", None),
        supergrok_auth_url=os.getenv("SUPERGROK_OAUTH_AUTH_URL", os.getenv("XAI_OAUTH_AUTH_URL", "https://auth.x.ai/oauth2/authorize")),
        supergrok_token_url=os.getenv("SUPERGROK_OAUTH_TOKEN_URL", os.getenv("XAI_OAUTH_TOKEN_URL", "https://auth.x.ai/oauth2/token")),
    )
    return OAuthManager(data_dir=args.data_dir, providers=providers)


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
