# handoff after supergrok oauth / xai api rename

locked decisions:
- package lives under tomo_core/ in the current repo
- telegram is dm-only
- no room_id in v1; actor_id is the session identity
- delivery emits model_probability(1, 4): at least 1 bubble, max 4 bubbles
- first bubble replies to the user's telegram message
- no tool execution status bubbles
- typing starts when the inbound message is accepted
- xai means the xai api key path
- supergrok oauth means the grok-build oauth path
- supergrok oauth and google calendar connect are available through telegram /connect

implemented now:
- InboundEnvelope with connector, actor_id, message_id, text, attachments, location, metadata
- session key: telegram:actor:<actor_id>
- JsonSessionStore for one logical user turn and one logical assistant turn
- ProviderAdapter protocol
- XaiApiProvider for XAI_API_KEY / TOMO_XAI_API_KEY
- SuperGrokOAuthProvider for explicit supergrok access tokens
- OAuthBackedSuperGrokProvider that reads the connected actor's saved supergrok token
- StaticProvider for tests and smoke mode
- langgraph-shaped graph builder with linear fallback if langgraph is missing locally
- PersonalAgentRuntime.handle_telegram_text
- TelegramDeliverySink and FakeTelegramClient
- TelegramBotApiClient with getUpdates, sendChatAction, sendMessage, answerCallbackQuery
- TelegramPollingBot with dm-only text routing, /connect, and callback_query routing
- DeliveryPlanner with plain-text cleanup and 1..4 bubble clamp
- `tomo_core.conversation` with `ConversationEngine.respond(ConversationRequest) -> ConversationResult`
- two normal model calls per valid turn: move selection, then soul-guided realization
- at most one repair call when realization violates the utterance contract
- compact assistant metadata records primary move, supporting moves, response goal, and confidence
- intentional utterances preserve one logical assistant turn while sending 1..4 Telegram bubbles
- SOUL.md loading
- OAuthManager with pkce begin, callback/code exchange, pending challenge storage, and token storage
- default google calendar oauth config with calendar.events scope
- configurable supergrok oauth endpoints via env
- hosted local/Daytona shared Telegram gateway, durable inbox, Railway/Daytona operations, and recovery runbooks documented in `../docs/`

next slices:

1. google calendar tools
- read saved token_google_calendar_<actor_id>.json
- list events can run directly
- create/update/delete require confirmation unless policy later marks the action safe
- add tests around token loading and calendar api calls

2. oauth refresh
- store expires_at on token save
- refresh access tokens when expired
- support google refresh_token flow
- support supergrok refresh flow once endpoint contract is confirmed

3. inbound reactions
- add pre-answer graph node after initial think
- sparse react false default
- skip slash commands and auth side flows

4. tools and memory
- add web search, geolocation, durable memory, and image support in separate tested slices
- keep tool execution invisible in normal telegram chat

5. deferred conversation infrastructure
- session identity, idempotent persistence, delivery retry duplication, durable memory, and deployment snapshot rollout remain separate work
- the production Daytona snapshot does not contain conversation moves until a later approved snapshot build and Railway rollout
