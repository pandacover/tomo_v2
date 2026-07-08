# Personal Agent Architecture Plan

> For Hermes: this is a planning artifact only. Do not implement from this file until the user confirms scope.

Goal: design the first architecture slice for a conversational personal agent with Telegram chat, web search, geolocation, Google Calendar, LangGraph orchestration, provider adapters starting with xAI Grok OAuth, soul/personality markdown, multimodal messaging, Telegram reactions, and conversational multi-bubble responses.

Architecture: keep the agent as one logical conversation turn stored in session history, while Telegram delivery may emit 1 to 4 physical message bubbles. Use LangGraph for the internal turn pipeline, provider adapters for model auth and invocation, platform adapters for Telegram-specific UX, and capability adapters for web/search/location/calendar tools. Separate reasoning, tool execution, and delivery expression so tool traces never leak as chat bubbles.

Tech stack: python, langgraph, langchain-compatible message models if useful, telegram bot api, xAI OAuth-backed provider adapter, google oauth/calendar api, geolocation provider abstraction, sqlite for local state and memory, optional object storage/filesystem for images.

---

## Core product rules

1. Telegram is the primary chat interface.
2. User-visible assistant messages are plain text, not markdown-formatted content.
3. The agent can receive images and send images.
4. Image generation is available when the selected provider/model supports image output.
5. The agent can react to Telegram messages before sending a reply.
6. The first message bubble in every assistant response stack replies directly to the triggering Telegram message.
7. Tool execution status is not shown as Telegram message bubbles.
8. Telegram typing status begins when the user message is accepted and refreshes until the turn completes.
9. The agent is optimized for conversation, not just task completion.
10. Every response is generated with awareness of the running conversation, current user message, retrieved memories, and soul/personality.

---

## Proposed layers

### 1. Gateway layer

Owns platform-specific transport.

Telegram gateway responsibilities:
- receive message updates, images, location shares, replies, commands, and reactions
- normalize native Telegram updates into an inbound envelope
- start typing immediately and refresh while the agent turn is active
- optionally set an inbound reaction before the agent completes
- send the delivery bubble stack
- ensure the first bubble uses reply_parameters pointing at the user's message_id
- hide tool traces by default
- upload outbound images when the final response contains media

InboundEnvelope fields:
- connector: "telegram"
- room_id: telegram chat id
- actor_id: telegram user id
- message_id: telegram message id
- timestamp
- text
- images
- location
- reply_to_message_id
- native_metadata

### 2. Conversation runtime layer

Owns session continuity.

Responsibilities:
- map connector + room_id to a durable conversation session
- append user turns, assistant turns, image metadata, and tool summaries
- build model-ready history from the running conversation
- maintain one logical assistant turn even when Telegram sends multiple bubbles
- provide short-term context window selection
- call memory retrieval before the graph turn
- persist any new memories after the turn only through explicit memory policy

Important rule:
Telegram may send multiple physical messages, but storage should save one assistant turn with structured delivery metadata.

### 3. LangGraph agent layer

Owns reasoning and tool orchestration.

Recommended graph shape:

user_input
→ load_context
→ load_soul
→ retrieve_memories
→ think
→ maybe_react
→ think_after_reaction
→ decide_tool_or_answer
→ optional_execute_task_loop
→ final_answer_draft
→ delivery_compose
→ validate_delivery
→ emit

Nodes:
- load_context: fetch running conversation, current user envelope, attachments, location, calendar connection status
- load_soul: load SOUL.md or equivalent personality text
- retrieve_memories: fetch scoped relevant memories
- think: private reasoning node, never emitted
- maybe_react: optionally select a Telegram reaction, sparse by default
- think_after_reaction: update plan after deciding whether to react
- decide_tool_or_answer: choose direct response or tool loop
- optional_execute_task_loop: run web/geolocation/calendar/image tools as needed
- final_answer_draft: produce one logical answer
- delivery_compose: convert logical answer into 1 to 4 plain-text bubbles
- validate_delivery: enforce no markdown, sentence caps, no tool traces, first bubble present
- emit: gateway-specific delivery

The user-proposed flow maps cleanly to this:
user_query + soul -> think -> optional add message reaction -> think -> {execute task -> respond} x model_probability(0, 4)

Refinement:
Use a delivery planner rather than random message count. The model can propose 1 to 4 bubbles, but a validator clamps it. Default should be 1 to 2 bubbles; 3 to 4 only for emotionally rich or multi-part answers.

### 4. Provider adapter layer

Owns model auth, model capabilities, and invocation shape.

ProviderAdapter interface:
- name
- auth_status()
- complete(messages, tools, model, options)
- stream(optional)
- supports_images_in
- supports_images_out
- supports_tool_calls
- supports_reactions? false, reactions are platform-side
- supports_reasoning_effort

Initial adapter:
- xai_grok_oauth

xAI Grok OAuth responsibilities:
- load OAuth token from secure local data dir
- refresh token when needed
- expose chat completion models
- expose image generation models if available through the configured model
- normalize errors into provider-independent exceptions

Future adapters:
- openai api key
- anthropic api key
- local model
- custom http model

### 5. Capability/tool layer

Tools should be plain capability adapters, not Telegram-aware.

Initial tools:
- web_search(query)
- geocode/place_search/reverse_geocode/current_nearby(location)
- google_calendar_list_events(time_window)
- google_calendar_create_event(event)
- google_calendar_update_event(event_id, patch)
- google_calendar_delete_event(event_id)
- memory_read(query)
- memory_write(candidate_memory)
- image_generate(prompt, model)

Tool policies:
- tool status is internal only
- final answer can mention completed work naturally
- tool errors are summarized in the final answer unless the failure requires direct user action
- sensitive tools like calendar writes require confirmation unless the user's intent is explicit and low-risk policy allows it

### 6. Soul layer

SOUL.md is the personality contract.

Usage:
- injected into core agent system prompt
- injected into delivery composer prompt
- not used as a place for mechanics like bubble counts or tool policies

Reason:
Voice and product behavior should be separate. Soul shapes tone; runtime enforces delivery rules.

### 7. Memory layer

Use two kinds of memory.

Short-term memory:
- current running conversation session
- recent messages selected by token budget
- current user message and attachments always included

Durable memory:
- sqlite table with memory rows
- fields: id, subject_id, scope, text, source, confidence, importance, created_at, updated_at, disabled_at, metadata_json
- local-first retrieval using sqlite fts5 before embeddings
- optional deterministic entity/alias expansion
- optional later reranking, isolated from visible chat output

Memory write policy:
- do not write every turn
- write stable preferences, identity facts, recurring routines, important relationships, and long-lived constraints
- avoid temporary task progress unless explicitly useful long term
- use contradiction/supersession rather than hard delete

Memory read policy:
- retrieve before each agent turn
- include only memories relevant to the current user message and session
- keep memory snippets compact

### 8. Image handling

Inbound images:
- Telegram gateway downloads image file or stores file_id reference
- create attachment metadata
- pass image content/url to provider only if selected model supports vision
- otherwise route to OCR/image-description tool if available

Outbound images:
- if response includes generated image artifact, gateway sends image as photo/document
- text bubbles still follow delivery policy
- first text bubble should still reply to the triggering user message when present

### 9. Google Calendar integration

Use maintainer-owned OAuth client or user-owned OAuth depending product scope.

Recommended product shape:
- shared app credentials configured by operator
- per-user token stored under data dir
- connect flow can start from dashboard or Telegram later
- calendar tools only bind when connected

Calendar tool permissions:
- read events without confirmation
- create/update/delete require confirmation unless intent is unambiguous and policy allows it

### 10. Geolocation

Inputs:
- live Telegram location share
- static Telegram location message
- explicit text place name

Storage:
- keep last known location per actor/room only if user consents or platform action implies consent
- store timestamp and precision

Tools:
- reverse geocode current coordinates
- search nearby places
- estimate routes later if needed

---

## Concrete implementation slices

### Slice 1: skeleton runtime

Objective: create the minimal runnable agent loop with Telegram inbound envelope, session storage, provider adapter interface, and one direct response path.

Acceptance:
- user sends Telegram text
- agent starts typing immediately
- graph loads soul + recent session
- xAI OAuth provider returns text
- one Telegram reply bubble is sent as a reply to the user message
- session stores one user turn and one assistant turn

### Slice 2: delivery planner

Objective: add conversational delivery as a post-agent step.

Acceptance:
- delivery planner outputs 1 to 4 plain-text bubbles
- validator strips markdown-like formatting
- first bubble replies to user message
- later bubbles are normal Telegram messages
- bubbles are paced
- no tool execution messages are sent

### Slice 3: inbound reactions

Objective: add optional pre-answer Telegram reaction.

Acceptance:
- graph chooses react false or one emoji
- reaction is sparse, not every turn
- reaction happens before final bubbles
- slash commands and approval flows skip reactions

### Slice 4: web search tool

Objective: bind web_search into LangGraph tool loop.

Acceptance:
- user can ask current-info question
- tool runs invisibly
- final answer summarizes result in natural voice
- no tool trace bubble appears

### Slice 5: geolocation

Objective: accept Telegram location and use it in responses/tools.

Acceptance:
- location message updates current location context
- user can ask “what’s near me”
- agent uses geolocation tool
- final response is conversational

### Slice 6: Google Calendar

Objective: connect calendar and expose read/create/update tools.

Acceptance:
- auth status is visible internally
- list events works after connect
- create event asks confirmation when needed
- successful writes are reflected in final response

### Slice 7: memories

Objective: add local durable memory read/write.

Acceptance:
- relevant durable memories are injected before think
- memory writes are policy-gated
- “remember that…” stores a memory
- later relevant turns retrieve it
- no nested reranker output leaks into chat

### Slice 8: image receive/send

Objective: support Telegram images and provider image capabilities.

Acceptance:
- inbound image gets attached to the turn
- model can answer about image when vision is supported
- image generation sends an actual Telegram image when supported
- fallback is honest when selected model cannot process/generate images

---

## Key decisions to lock now

1. Use one inbound envelope abstraction now, even if Telegram is the only gateway.
2. Store one logical assistant turn, with delivery metadata for bubbles.
3. Keep tools invisible unless debug mode is explicitly enabled later.
4. Treat soul as voice, not runtime mechanics.
5. Use provider capability flags so image and tool support are model-dependent.
6. Use local sqlite + fts5 for memories first; embeddings are later.
7. Delivery composition should be model-assisted but validator-enforced.
8. The running conversation is always first-class context, not an optional memory lookup.

---

## Open questions

1. Should durable memory be per telegram actor_id, per room_id, or both?
2. Should group chats be supported now, or only DMs first?
3. Should Google Calendar connect happen through Telegram, dashboard, or CLI first?
4. Should image generation be a tool available to all models, or only auto-bound when provider reports support?
5. Should delivery max be 3 bubbles or 4? User proposed model_probability(0, 4), but the current UX likely feels better at 1 to 3 with 4 as rare.
6. Should the agent ever proactively message, or only respond to inbound user messages in this first slice?

---

## Validation checklist

- send plain Telegram text and receive a reply-threaded first bubble
- verify typing starts immediately and stops after final bubble
- verify no markdown formatting in assistant bubbles
- verify no tool status bubble during a web search turn
- verify session history contains current and previous turns
- verify soul changes affect tone without changing runtime rules
- verify reaction is optional and sparse
- verify 1 to 2 bubbles are the default for normal answers
- verify calendar read works after OAuth connect
- verify calendar write confirmation behavior
- verify location share can be used in later message context
- verify inbound image path works with a vision-capable model
- verify image generation path works only when selected model supports it

---

## Recommended next move

Implement slice 1 and slice 2 before adding more tools. The main risk is not lack of capabilities; it is shipping a tool-first bot that feels non-conversational. The first milestone should prove the conversation loop, soul injection, session continuity, typing, reply threading, and intentional bubble delivery feel right before calendar/memory complexity lands.
