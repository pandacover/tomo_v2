# tomo core

telegram-first personal agent core.

what is included:
- dm-only telegram bot long polling
- telegram inbound envelope keyed by actor_id, no room_id
- immediate typing action before the agent turn
- /connect inline menu for supergrok oauth and google calendar
- oauth pkce begin/complete flow through telegram dm
- per-telegram-actor oauth token storage under .tomo_core/oauth
- xai api provider for XAI_API_KEY / TOMO_XAI_API_KEY
- supergrok oauth provider that uses the connected actor's token when available
- google calendar oauth connection storage
- static provider fallback for local bot smoke tests
- langgraph-shaped turn graph, with a linear fallback when langgraph is not installed
- soul injection into the model messages
- two-stage conversational loop: soul-aware move selection followed by move-specific realization
- one primary conversational move plus up to two supporting moves
- 1-4 intentional telegram utterances, each capped at three sentences
- compact move metadata stored with one logical assistant turn
- malformed realization receives one bounded repair attempt
- delivery planner that emits 1 to 4 plain-text bubbles
- first bubble replies to the triggering telegram message
- no tool execution message bubbles

what is intentionally not included yet:
- google calendar event tools
- web search tools
- geolocation tools
- durable agent memories
- image receive/send plumbing
- reactions

## run tests

```bash
cd /c/Users/luvma/OneDrive/Desktop/zero_labs/tomo_v2/tomo_core
PYTHONPATH=src python -m unittest discover -s tests -v
```

## hosted operations

For the shared Telegram gateway on Railway and per-user Daytona sandboxes, see [../docs/daytona-railway.md](../docs/daytona-railway.md). User onboarding and local hosted-mode setup are in [../docs/onboarding-telegram.md](../docs/onboarding-telegram.md).

## run a real telegram bot

create a bot with botfather and copy the bot token.

```bash
cd /c/Users/luvma/OneDrive/Desktop/zero_labs/tomo_v2/tomo_core
TELEGRAM_BOT_TOKEN='123:abc' PYTHONPATH=src python -m tomo_core.cli telegram start
```

## xai api key mode

this is the plain xai api path. get the api key from the xai console and run:

```bash
TELEGRAM_BOT_TOKEN='123:abc' \
XAI_API_KEY='xai-api-key' \
PYTHONPATH=src python -m tomo_core.cli telegram start
```

## grok login mode

for local testing, you probably do not need a supergrok client id. use the same cached login as the grok cli:

```bash
grok login
# or for headless:
grok login --device-auth

TELEGRAM_BOT_TOKEN='123:abc' \
PYTHONPATH=src python -m tomo_core.cli telegram start --use-grok-login
```

this reads `~/.grok/auth.json`. you can override the path with `GROK_AUTH_JSON` or `TOMO_GROK_AUTH_JSON`.

## supergrok oauth + google calendar /connect

supergrok oauth now uses the same public xai oauth client id/scopes as `uv run tomo login` from the main tomo repo. you do not need to provide a supergrok client id for local testing. google calendar still needs google oauth client ids before starting the bot:

```bash
TELEGRAM_BOT_TOKEN='123:abc' \
GOOGLE_OAUTH_CLIENT_ID='google-client-id' \
GOOGLE_OAUTH_CLIENT_SECRET='google-client-secret' \
PYTHONPATH=src python -m tomo_core.cli telegram start
```

then dm the bot:

```text
/connect
```

pick either:
- supergrok oauth
- google calendar

when you click supergrok oauth, the bot creates the same auth.x.ai pkce login URL shape as `uv run tomo login`. open the auth url, approve it, and paste the redirected callback url back into the telegram dm.

if `~/.grok/auth.json` exists and the inline oauth setup ever fails, the bot can also import that cached grok login into `.tomo_core/oauth` as a fallback.

tokens are saved under:

```text
.tomo_core/oauth/token_supergrok_<actor_id>.json
.tomo_core/oauth/token_google_calendar_<actor_id>.json
```

notes:
- google uses calendar.events scope.
- supergrok oauth endpoints default to https://auth.x.ai/oauth2/authorize and https://auth.x.ai/oauth2/token. override with SUPERGROK_OAUTH_AUTH_URL and SUPERGROK_OAUTH_TOKEN_URL if needed.
- if XAI_API_KEY is set, normal chat uses xai api key mode.
- if XAI_API_KEY is not set, normal chat uses the actor's saved supergrok oauth token.
- if neither is connected, normal chat replies with /connect guidance unless TOMO_CORE_STATIC_RESPONSE is set.

## local static smoke

```bash
TELEGRAM_BOT_TOKEN='123:abc' TOMO_CORE_STATIC_RESPONSE='yo. telegram is wired.' PYTHONPATH=src python -m tomo_core.cli telegram start
```

## next handoff

1. add google calendar event tools using the saved google_calendar token.
2. add token refresh for supergrok and google when access tokens expire.
3. add inbound reactions as a pre-answer graph node.
4. add web search, geolocation, durable memory, and image support as separate slices.
