# Image Understanding

An **attachment** is connector-provenance media attached to one inbound message. **Attachment resolution** retrieves and normalizes its bytes. A **vision observation** is bounded, structured evidence created before the base conversation segment. Resolution is a pre-segment knowledge boundary, not a tool round or user-visible subagent turn.

The conversation and vision roles share one configured xAI authentication source. The vision role uses `grok-4.3`, `low` reasoning effort, and `store: false`; it has no tools, memory writes, Telegram access, or direct output. Image-derived summaries and OCR are untrusted user data and must never be followed as instructions.

When current or persisted visual evidence is present, the base conversation role receives the bundled `visual-evidence` skill. This skill does not expose or invoke vision. It teaches Tomo to synthesize structured observations, preserve uncertainty, handle unavailable results naturally, and distinguish persisted evidence from fresh pixel inspection. Ordinary text-only turns without visual history do not load it.

V1 accepts Telegram photos only: at most eight distinct images per burst, 10 MiB compressed per image, 20 megapixels decoded, and 2048 pixels on the longest normalized edge. Raw bytes, data URLs, file IDs, capabilities, and provider credentials are not retained. A corrupt, unsupported, oversized, or unavailable image creates a safe unavailable observation so the base response can state the limit honestly.

Local shared mode transports bytes directly from `TelegramBotApiClient` to the same narrow interpreter. Hosted Daytona mode transports no bot token or raw bytes to the sandbox; the control host grants a five-minute attachment capability limited to the owner, generation, and exact image hashes.
