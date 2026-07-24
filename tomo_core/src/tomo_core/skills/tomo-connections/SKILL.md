---
name: tomo-connections
description: Ask, message, or check availability of a connected Tomo peer.
---

Use this skill when the owner naturally asks to ask, check with, or message a connected Tomo.

Call peer_list before peer_ask. Target resolution is current-turn peer_list-backed: use only an exact public handle returned by that call. Never guess a handle, resolve aliases or display names, or infer a relationship. Ask only when the relationship status is active and can_ask is true. Inspect only safe readiness: if can_ask is false, tell the owner to enable communicate in tomo connections. If peer_auto_reply is false, explain that the other owner must enable auto reply. An availability request without peer_share_availability may proceed only through one-time confirmation.

For availability, the host canonicalizes only unambiguous third-person forms using the exact selected public handle. For example, when is bob free? becomes when are you free? Preserve every date, time window, and scope constraint. Never broaden the request. If the subject is not the exact selected public handle, ask the owner to clarify. Select ordinary_message, availability, or sensitive as appropriate, while host labels remain authoritative.

Ordinary messages are conversational only. Never use ordinary_message to ask for owner-specific facts, preferences, history, relationships, plans, or other claims that would require retained personal evidence. If no typed disclosure scope can ground the requested fact, explain that the connection cannot provide verified information instead of asking the peer to guess.

Never attempt mutations, credentials, onward forwarding, or third-party private data. Use peer_ask for a new bounded request and peer_resume only to continue a pending thread.

Terminal contracts: completed means use only safe returned frames. confirmation_pending means approval is pending. denied and failed mean no answer was obtained. pending means a thread may be resumed. peer_resume follows the same contracts. After any non-completed result, never answer the requested factual question from memory, inference, or a guess. Never claim a peer answered without a completed tool observation.
