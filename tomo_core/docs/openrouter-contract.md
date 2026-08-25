# OpenRouter conversation contract

Tomo keeps ordinary turns on streaming JSONL. A segment may emit a `turn_plan`, memory controls, multiple `frame` records, and native tool calls before a later model segment continues from the tool observations. This preserves message -> think/tool -> message flow; a single top-level JSON object would not.

## Request policy

- OpenRouter requests ask for streamed usage so attempt-level input, output, and reasoning token counts remain measurable.
- Reasoning uses OpenRouter's normalized `reasoning` object and excludes reasoning text from the returned stream. Ordinary turns retain their configured effort.
- Requests with tools require an endpoint that supports the supplied parameters.
- A repair request uses strict `json_schema`, requires a schema-capable endpoint, limits the completion to 4096 tokens, and lowers reasoning effort because formatting a frame batch is not a reasoning task.

## Accepted SSE lifecycle

The adapter accepts arbitrary byte boundaries, CRLF or LF event separators, SSE comments, multi-line `data:` events, usage-only chunks, one choice per generation chunk, and an optional final `[DONE]` sentinel. A non-empty `finish_reason` is the authoritative terminal event. `[DONE]` supplies `stop` only when the provider omitted a finish reason.

The adapter rejects malformed JSON, non-SSE payloads, provider error events, multiple choices, invalid usage, conflicting or incomplete tool calls, duplicate completion, data after completion, and streams with neither a finish reason nor `[DONE]`. Transport, provider-protocol, and conversation-contract errors keep separate stable codes; exception text is never converted into a user-visible code.

## One bounded repair

Before any frame is visible, one failed model segment may be replaced once. Output-only failures use one strict response schema containing only `frames`. That schema can return one to three messages and cannot invoke tools or add reaction metadata. Reactions remain available on the normal JSONL path, but a reaction can never make the reply recovery fail.

Tool-specific failures keep the existing tool-aware replacement prompt so an intended tool action is not silently converted into prose. The global `max_contract_repairs` budget still limits the whole turn to one replacement. If the replacement also fails, the turn stops; there is no reaction-specific third attempt.

## Fixtures and measurement

`tests/fixtures/openrouter/` contains privacy-safe, synthetic SSE fixtures matching the shapes observed during the cutover: reasoning-only output, prose preamble, fenced JSONL, multiple frames, fragmented tool continuation, and malformed SSE. They contain no prompts, user content, identifiers, or credentials.

Production latency events record attempt number, repair number, time to first text/frame, input tokens, output tokens, and reasoning tokens. These fields are sufficient to compare the normal JSONL path with the single structured repair without logging model content.

References: [OpenRouter streaming](https://openrouter.ai/docs/api_reference/streaming), [reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), and [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs).
