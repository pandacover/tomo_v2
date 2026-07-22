# Inter-Owner Tomo Communication and Permissions Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Let Tomos belonging to different people communicate through an owner-consented, centrally brokered exchange while ensuring that foreign Tomos never inherit authority, invoke another owner’s tools, or bypass disclosure and confirmation policy.

**Architecture:** Add a deep `PeerExchange` module to the always-on control plane. It owns public handles, bilateral relationships, directional grants, bounded threads, requests, pending confirmations, idempotency, leases, and audit metadata. A short-lived owner/generation-scoped capability exposes a small `peer_ask` tool inside an interactive sandbox; a durable host worker executes the target Tomo as a new restricted `PeerTurn` with read-only personal search, no peer recursion, no memory writes, no reactions, and no mutating tools. Target frames return as a tool observation to the initiating Tomo. The core broker stores only deliberately disclosed exchange content, never either owner’s private memory or credentials.

**Tech Stack:** Python 3.11+, dataclasses, SQLite/WAL, FastAPI/Pydantic, existing HMAC capability pattern, Daytona sandbox dispatch, Tomo TurnRun protocol, Next.js 16/Bun dashboard, unittest, Bun test.

---

## 1. Product boundary and locked defaults

### Shipped vs planned

| Area | Shipped today | Planned here |
|---|---|---|
| Hosted identity | Better Auth user id maps to one `tomo_id`; Telegram installation binds chat, actor, and `tomo_id` | User-selected public Tomo handle without exposing Better Auth ids |
| Isolation | One Daytona sandbox and volume per hosted `tomo_id`; owner-scoped personal SQLite inside the volume | Keep personal stores isolated; add a separate minimal host broker DB |
| Connectors | Telegram private chats only; `InboundEnvelope` has connector/actor but no `room_id` | Peer exchange is an internal source, not a Telegram impersonation and not a new connector room |
| Runtime inputs | Interactive `InputBurst` and unattended `AutomationTurn` | Restricted `PeerTurn` with its own lifecycle and session key |
| Tools | Personal search is mandatory; cron mutations use short-lived host capability; unattended runs block non-safe tools | Interactive `peer_ask`; target PeerTurn gets read-only personal search only |
| Confirmation | Permanent memory deletion has pending confirmation; blocked automation tools return `approval_needed` but cannot resume | Durable peer confirmations tied to one proposal hash, owner, relationship revision, and expiry |
| Agent relationships | None | Invitation, bilateral acceptance, directional grants, revoke/block, audit |
| Persona | One `SOUL.md` | **No SOUL changes** |

### Locked v1 decisions

1. V1 supports different Better Auth owners on the same hosted Tomo control plane. Internet federation between independent deployments is parked.
2. A foreign Tomo message is untrusted evidence or a request, never user authority.
3. Both owners explicitly accept an **agent relationship** before any autonomous exchange.
4. Grants are directional. Alice allowing Bob’s Tomo to receive Alice’s availability does not grant the reverse.
5. V1 grants only `ordinary_message` and `availability`. Exact calendar entries, location, files, raw messages, health, finance, legal data, credentials, and third-party data always require a specific confirmation or remain forbidden.
6. V1 Tomos may converse and negotiate, but cannot make external commitments or execute mutating tools for a peer. Meeting booking, purchasing, sending, deleting, access changes, and account changes remain proposals for the affected owner’s interactive turn.
7. `peer_ask` is synchronous from the model’s perspective but implemented as durable request + worker + bounded polling. One ask produces one target response. The target cannot call `peer_ask`, preventing recursive loops.
8. Default limits: 4 requests per thread, 2,000 characters per request/response, 15-minute thread TTL, 60-second request execution deadline, 10 requests per relationship per hour.
9. Central exchange content retention is 30 days. Revocation stops future access immediately but cannot retract content already disclosed. Permanent purge requires exact-target confirmation.
10. Every peer-generated message is visibly identified as coming from another Tomo in inspection/audit surfaces. It is never presented as the human speaking directly.

### Permission and confirmation matrix

| Operation | Requirement |
|---|---|
| Discover exact public handle | Authenticated dashboard owner; rate limited |
| Invite another Tomo | Initiator confirmation in dashboard |
| Accept relationship | Recipient confirmation in dashboard |
| Revoke or block relationship | Immediate owner action; no second confirmation because it reduces authority |
| Ordinary peer request/reply | Active bilateral relationship plus sender `communicate` and recipient `auto_reply` grants |
| Share coarse availability | Directional `share_availability` grant for the stated scheduling purpose |
| Read own private context to decide | Allowed internally; does not itself grant disclosure |
| Exact calendar details or sensitive/private content | Specific pending confirmation from the owner whose data would leave |
| Proposed meeting/commitment | Proposal may be exchanged; execution requires affected owner confirmation unless a future standing action grant is designed |
| Mutating tool, payment, purchase, send, delete, booking, access/security change | Never executable by PeerTurn in v1 |
| Add a third person or forward content | New relationship/recipient permission; existing grant is not transitive |
| Import peer claim into trusted personal memory | Never automatic in PeerTurn; later interactive owner acceptance required |
| Credentials, tokens, authentication secrets | Forbidden, not confirmable through peer exchange |
| Permanent relationship/thread data purge | Exact target plus explicit confirmation |

## 2. End-to-end runtime lifecycle

1. Alice asks her Tomo, “ask bob’s Tomo when he is free Friday.” The Telegram burst is durably accepted and fenced normally.
2. Alice’s interactive sandbox receives a five-minute `PeerCapability` bound to Alice’s owner, actor, chat destination, session, and generation.
3. Alice’s Tomo calls `peer_ask(handle="bob", purpose="schedule", disclosure_kind="availability", message="...")`.
4. The broker authenticates the capability, re-checks that Alice’s generation is active, resolves Bob without returning Bob’s private identifiers, verifies relationship and both directional grant revisions, enforces quotas, and idempotently creates one request.
5. If Bob has not granted `share_availability`, the broker creates a bounded pending confirmation, sends Bob a deterministic Telegram notice, and returns `needs_owner_confirmation` to Alice’s Tomo. No Bob sandbox execution occurs.
6. If allowed, the peer worker leases the request, re-checks grants/revocation, resolves Bob’s Telegram installation and Daytona sandbox, and runs a `PeerTurn` in session `peer:<relationship_id>:<thread_id>`.
7. Bob’s runtime hydrates bounded owner memories and may use read-only personal search. The peer payload is marked untrusted. Memory controls, reactions, cron tools, peer tools, attachment access, and all mutating tools are disabled.
8. Bob’s validated frames are accepted into the peer session and returned to the host. The broker scans for secret-shaped material, enforces size/count limits, stores the disclosed response, and completes the request.
9. Alice’s `PeerApiClient` polls by request id and receives a bounded tool observation containing status, peer handle, thread id, and response frames. It never receives Bob’s owner id, memory ids, source transcripts, or tool observations.
10. Alice’s original TurnRun continues and responds to Alice. A later ask may continue the same thread if limits and grants remain valid.
11. If Alice’s generation is superseded before broker acceptance, creation fails closed. Once Bob’s response is accepted, it cannot be perfectly retracted; idempotency prevents duplicate execution.
12. Relationship revocation, grant revision changes, expiry, worker retry, and stale leases are checked again before every target execution and completion write.

## 3. Implementation tasks

### Task 1: Add canonical peer vocabulary

**Objective:** Record product meaning before introducing schemas.

**Files:**
- Modify: `tomo_core/CONTEXT.md:7-14,42-52,67-87`
- Test: `tomo_core/tests/test_context.py`

**Step 1: Write the failing glossary test**

Assert that `CONTEXT.md` defines: `peer Tomo`, `agent relationship`, `relationship grant`, `inter-agent thread`, `peer request`, `commitment proposal`, and `pending peer confirmation`; also assert that `owner`, `actor`, `tomo_id`, and `SOUL.md` meanings are not redefined.

**Step 2: Run RED**

```bash
cd tomo_core
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests -p test_context.py
```

Expected: FAIL because peer terms are absent.

**Step 3: Add definitions**

Use implementation-free glossary entries. Include the invariant: “a peer Tomo can request or disclose, but cannot grant authority for either human owner.”

**Step 4: Run GREEN**

Expected: all context tests pass.

**Step 5: Optional commit only if explicitly requested**

```bash
git add tomo_core/CONTEXT.md tomo_core/tests/test_context.py
git commit -m "docs(domain): define inter-owner tomo exchange"
```

### Task 2: Define immutable peer domain models

**Objective:** Create bounded types with no persistence or HTTP concerns.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_models.py`
- Create: `tomo_core/tests/test_peer_models.py`

**Step 1: Write failing tests**

Cover normalized handles, unordered relationship pair uniqueness, directional grants, thread TTL, request size, allowed v1 disclosure kinds, terminal-state immutability, and rejection of booleans as integers.

**Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests -p test_peer_models.py
```

Expected: import failure.

**Step 3: Implement the minimal model surface**

```python
class DisclosureKind(str, Enum):
    ORDINARY_MESSAGE = "ordinary_message"
    AVAILABILITY = "availability"
    SENSITIVE = "sensitive"

class RelationshipStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    BLOCKED = "blocked"

@dataclass(frozen=True)
class RelationshipGrant:
    relationship_id: str
    grantor_owner_id: str
    grantee_owner_id: str
    communicate: bool
    auto_reply: bool
    share_availability: bool
    revision: int
    expires_at: datetime | None
```

Add `PeerThread`, `PeerRequest`, `PeerResponse`, and `PendingPeerConfirmation`. Keep public handles separate from owner ids.

**Step 4: Run GREEN**

Expected: model tests pass.

### Task 3: Implement pure directional permission policy

**Objective:** Centralize every allow/confirm/deny decision behind one small interface.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_policy.py`
- Create: `tomo_core/tests/test_peer_policy.py`

**Step 1: Write a table-driven failing test**

Cases must include inactive relationship, wrong direction, expired grant, ordinary allowed, availability allowed, sensitive confirmation, commitment confirmation, forbidden credentials, third-party denial, and all mutating actions denied in PeerTurn.

**Step 2: Implement the interface**

```python
@dataclass(frozen=True)
class PeerPolicyDecision:
    outcome: Literal["allow", "confirm", "deny"]
    reason: str

class PeerPolicy:
    def decide(self, relationship, sender_grant, recipient_grant, request, *, now) -> PeerPolicyDecision:
        ...
```

Reason codes must be fixed safe values. Do not return private data in errors.

**Step 3: Run GREEN**

Expected: every matrix row passes.

### Task 4: Add the central peer SQLite repository

**Objective:** Persist broker state independently from personal sandbox databases.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_store.py`
- Create: `tomo_core/tests/test_peer_store.py`

**Step 1: Write RED tests**

Test migrations, unique case-insensitive handles, unordered owner-pair uniqueness, bilateral acceptance, directional grant revisions, idempotent request creation, leases, retry recovery, 30-day pruning, owner filters, and concurrent SQLite writers.

**Step 2: Add schema version 1**

Tables:

```sql
peer_handles(owner_id primary key, handle unique, display_name, created_at, updated_at)
peer_relationships(relationship_id primary key, owner_low, owner_high, invited_by, status, revision, created_at, updated_at)
peer_grants(relationship_id, grantor_owner_id, grantee_owner_id, communicate, auto_reply, share_availability, revision, expires_at, updated_at, primary key(relationship_id, grantor_owner_id))
peer_threads(thread_id primary key, relationship_id, purpose, status, request_count, expires_at, created_at, updated_at)
peer_requests(request_id primary key, thread_id, sender_owner_id, recipient_owner_id, disclosure_kind, message, source_generation_id, source_call_id, status, attempts, available_at, lease_token, lease_until, error_code, created_at, updated_at, unique(sender_owner_id, source_generation_id, source_call_id))
peer_responses(response_id primary key, request_id unique, frames_json, status, created_at)
peer_pending_confirmations(pending_id primary key, request_id unique, owner_id, action_kind, payload_hash, preview, status, expires_at, created_at, decided_at)
peer_events(event_id integer primary key, relationship_id, owner_id, action, metadata_json, created_at)
```

Never store Better Auth session tokens, model tokens, private memory ids, or raw personal-search results.

**Step 3: Run GREEN**

Expected: repository tests pass including owner-scope leak tests.

### Task 5: Implement relationship and request broker

**Objective:** Put policy, quotas, idempotency, and repository transitions behind one deep module.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_exchange.py`
- Create: `tomo_core/tests/test_peer_exchange.py`

**Step 1: Write failing scenario tests**

Cover invite/accept, independent grant updates, blocked peer, duplicate ask, rate limit, confirmation creation, stale grant revision, revoke during lease, completion, safe failure, and non-retraction semantics.

**Step 2: Implement the small interface**

```python
class PeerExchange:
    def invite(self, owner_id: str, peer_handle: str) -> Relationship: ...
    def accept(self, owner_id: str, relationship_id: str) -> Relationship: ...
    def update_grant(self, owner_id: str, relationship_id: str, patch: GrantPatch, revision: int) -> RelationshipGrant: ...
    def submit(self, capability: PeerCapability, command: PeerAskCommand) -> PeerSubmitResult: ...
    def claim_next(self, worker_id: str, now: datetime) -> PeerExecutionClaim | None: ...
    def complete(self, claim: PeerExecutionClaim, frames: tuple[str, ...]) -> None: ...
```

Hide SQL, handle lookup, pair ordering, quota counters, leases, and policy evaluation inside.

**Step 3: Run GREEN**

Expected: scenario tests pass.

### Task 6: Add owner/generation-scoped peer capabilities

**Objective:** Prevent sandboxes from impersonating owners or using durable control credentials.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_capability.py`
- Create: `tomo_core/tests/test_peer_capability.py`

**Step 1: Write RED tests**

Cover signature tampering, wrong owner/actor/chat/session/generation, expiry over five minutes, future issue time, unknown operation, and private key permissions.

**Step 2: Mirror the proven capability shape**

```python
@dataclass(frozen=True)
class PeerCapability:
    owner_id: str
    actor_id: str
    destination: str
    session_id: str
    generation_id: str
    operations: tuple[str, ...]
    issued_at: int
    expires_at: int
```

Operations for v1: `list_relationships`, `ask`, `inspect_request`, `decide_confirmation`. Use a new signing key file, never the cron or attachment key.

**Step 3: Run GREEN**

Expected: capability tests pass.

### Task 7: Expose peer management and capability routes

**Objective:** Add authenticated dashboard routes and capability-authenticated sandbox routes without bloating `control_api.py`.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_api.py`
- Modify: `tomo_core/src/tomo_core/control_api.py:127-154,311`
- Create: `tomo_core/tests/test_peer_api.py`
- Modify: `tomo_core/tests/test_control_api.py`

**Step 1: Write RED endpoint tests**

Dashboard/BFF operations:

```text
PUT    /v1/peers/me/handle
GET    /v1/peers/relationships
POST   /v1/peers/invitations
POST   /v1/peers/relationships/{id}/accept
PATCH  /v1/peers/relationships/{id}/grant
POST   /v1/peers/relationships/{id}/revoke
POST   /v1/peers/relationships/{id}/block
GET    /v1/peers/threads/{id}
```

Sandbox operations:

```text
GET  /v1/peer-agent/relationships
POST /v1/peer-agent/requests
GET  /v1/peer-agent/requests/{id}
POST /v1/peer-agent/confirmations/{id}/decision
```

Test BFF API-key authentication separately from HMAC capability authentication. Assert one owner cannot enumerate another owner’s relationships or thread.

**Step 2: Implement `build_peer_router(...) -> APIRouter`**

Inject `PeerExchange`, clock, capability verifier, and generation-activity callback. Do not instantiate global mutable state at import time.

**Step 3: Run GREEN**

Expected: peer and existing control API tests pass.

### Task 8: Build the bounded sandbox peer client and tool registry

**Objective:** Give interactive Tomo only the broker operations authorized for its current generation.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_tools.py`
- Create: `tomo_core/tests/test_peer_tools.py`

**Step 1: Write RED tests**

Assert exact JSON schemas, URL quoting, headers, timeouts, bounded responses, no secret reflection, and tool flags.

**Step 2: Implement**

```python
class PeerApiClient:
    def list_relationships(self) -> dict[str, object]: ...
    def ask(self, arguments: dict[str, object]) -> dict[str, object]: ...


def peer_registry(client: PeerApiClient, generation_id: str) -> ToolRegistry:
    return ToolRegistry((
        BoundTool(ToolSpec("peer_list", "List active owner-approved Tomo relationships.", LIST_SCHEMA, unattended_safe=False), ...),
        BoundTool(ToolSpec("peer_ask", "Ask one approved peer Tomo a bounded question.", ASK_SCHEMA, read_only=False, parallel_safe=False, unattended_safe=False), ...),
    ))
```

`peer_ask` schema requires `peer_handle`, `purpose`, `disclosure_kind`, `message`, optional `thread_id`, and `call_id`. Do not expose owner ids.

**Step 3: Run GREEN**

Expected: peer tool tests pass.

### Task 9: Bind peer tools through every active runtime path

**Objective:** Ensure the tool is actually available in hosted Daytona and local shared-gateway execution.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_dispatch.py:70-98,159-175`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py:53-61`
- Modify: `tomo_core/src/tomo_core/cli.py:250-325,519-566`
- Modify: `tomo_core/src/tomo_core/shared_gateway.py:48-125`
- Modify: `tomo_core/tests/test_sandbox_dispatch.py`
- Modify: `tomo_core/tests/test_sandbox_inbound.py`
- Modify: `tomo_core/tests/test_shared_gateway.py`
- Modify: `tomo_core/tests/test_cli.py`

**Step 1: Write RED tests**

Assert that an interactive generation receives a five-minute peer capability and `peer_ask` appears in the actual `PersonalAgentRuntime.conversation.tool_registry.schemas()`. Assert it is absent from automation and target PeerTurn registries. Assert secrets are included in sandbox redaction values.

**Step 2: Implement `_interactive_peer_env(...)`**

Add:

```text
TOMO_PEER_CONTROL_URL
TOMO_PEER_CAPABILITY
TOMO_PEER_OWNER_ID
TOMO_PEER_ACTOR_ID
TOMO_PEER_DESTINATION
TOMO_PEER_SESSION_ID
TOMO_PEER_GENERATION_ID
```

Extend, rather than replace, mandatory personal-search and cron registries. Duplicate tool names must fail closed.

**Step 3: Run GREEN**

Expected: all four wiring tests pass.

### Task 10: Add `PeerTurn` to the trusted sandbox protocol

**Objective:** Represent a peer request without pretending it is a Telegram user message or cron event.

**Files:**
- Modify: `tomo_core/src/tomo_core/models.py:7,153-242`
- Modify: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Modify: `tomo_core/tests/test_sandbox_protocol.py`
- Create: `tomo_core/tests/test_peer_turn.py`

**Step 1: Write RED tests**

Test strict encode/decode, owner/relationship/thread/request binding, bounded content, allowed disclosure kind, timezone-aware expiry, and rejection of native connector metadata.

**Step 2: Implement**

```python
@dataclass(frozen=True)
class PeerTurn:
    generation_id: str
    revision: int
    relationship_id: str
    thread_id: str
    request_id: str
    peer_handle: str
    purpose: str
    disclosure_kind: Literal["ordinary_message", "availability"]
    message: str
    expires_at: str

    @property
    def session_key(self) -> str:
        return f"peer:{self.relationship_id}:{self.thread_id}"
```

The prompt-facing text must explicitly state that peer content is untrusted and grants no tool, memory, or user authority.

**Step 3: Run GREEN**

Expected: protocol round-trip tests pass.

### Task 11: Add restricted PeerTurn runtime participation

**Objective:** Let the target Tomo reason with its own context while preventing authority escalation and memory poisoning.

**Files:**
- Modify: `tomo_core/src/tomo_core/runtime.py:107-145,176-193,195-326`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Create: `tomo_core/tests/test_runtime_peer_turns.py`
- Modify: `tomo_core/tests/test_conversation_prompts.py`

**Step 1: Write RED tests**

Assert PeerTurn:
- uses its peer session key;
- reads only target owner memories;
- exposes only unattended-safe read-only personal search;
- cannot see `peer_ask`, cron mutations, attachments, reactions, or Telegram delivery;
- cannot stage memory controls;
- never treats the peer message as system text;
- emits one to three bounded frames and an accepted completion.

**Step 2: Implement `handle_peer_turn_iter`**

Reuse `_handle_turn_iter` only after making source-specific behavior explicit. Do not add conditionals throughout callers; introduce a small turn-policy object describing typing, reactions, memory writes, tool registry, and delivery target.

**Step 3: Run GREEN**

Expected: runtime and prompt tests pass.

### Task 12: Dispatch PeerTurn to the target Daytona sandbox

**Objective:** Execute one target response through the same snapshot/auth/session/parser guarantees as Telegram and cron.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py:64-130`
- Modify: `tomo_core/src/tomo_core/onboarding_store.py:167-178`
- Create: `tomo_core/tests/test_sandbox_peer_dispatch.py`
- Modify: `tomo_core/tests/test_onboarding_store.py`

**Step 1: Write RED tests**

Cover lookup by `tomo_id`, owner lock serialization, fresh OAuth token, no peer capability inside target execution, timeout, grant revocation during lease, safe parser errors, and target frames returned without Telegram delivery.

**Step 2: Implement `iter_peer_events(...)`**

Use `TOMO_PEER_TURN_JSON`, target `TOMO_INSTANCE_ID`, and the existing sandbox snapshot. Bind `is_active` to the peer request lease and grant revision. Do not reserve a Telegram generation or send typing.

**Step 3: Run GREEN**

Expected: dispatch tests pass.

### Task 13: Add the durable peer worker

**Objective:** Claim queued requests, run target PeerTurn, and complete them exactly once.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_service.py`
- Create: `tomo_core/tests/test_peer_service.py`
- Modify: `tomo_core/src/tomo_core/cli.py:320-347`
- Modify: `tomo_core/tests/test_railway_core_start.py`

**Step 1: Write RED tests**

Test lease acquisition, heartbeat/retry, target installation unavailable, relationship revoked after claim, timeout, duplicate worker, success completion, max attempts, and clean start/stop with the shared gateway.

**Step 2: Implement a scheduler-shaped service**

Follow `CronSchedulerService` lifecycle conventions but keep peer request status independent from cron. Start it before Telegram polling and stop it in `finally`.

**Step 3: Run GREEN**

Expected: worker and start wiring tests pass.

### Task 14: Return durable peer results to the initiating tool call

**Objective:** Poll boundedly without holding private state in memory or fabricating absence.

**Files:**
- Modify: `tomo_core/src/tomo_core/peer_tools.py`
- Modify: `tomo_core/src/tomo_core/peer_api.py`
- Modify: `tomo_core/tests/test_peer_tools.py`
- Modify: `tomo_core/tests/test_peer_api.py`

**Step 1: Write RED tests**

Cover immediate success, pending confirmation, timeout, peer failure, revoked relationship, superseded source generation, and idempotent retry returning the same result.

**Step 2: Implement bounded polling**

Poll at 250–500 ms for at most 60 seconds. Return only:

```json
{
  "ok": true,
  "status": "completed",
  "peer_handle": "bob",
  "thread_id": "...",
  "frames": ["..."]
}
```

For pending confirmation, return safe status and expiry, not the recipient’s private identity or internal policy.

**Step 3: Run GREEN**

Expected: result tests pass.

### Task 15: Implement pending peer confirmation in the owner’s normal turn

**Objective:** Resolve sensitive disclosure requests only from direct owner language, never a peer message.

**Files:**
- Create: `tomo_core/src/tomo_core/peer_governance.py`
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Create: `tomo_core/tests/test_peer_governance.py`
- Modify: `tomo_core/tests/test_conversation_parsing.py`
- Modify: `tomo_core/tests/test_runtime_peer_turns.py`

**Step 1: Write RED tests**

Test exact owner binding, expiry, proposal hash, relationship revision, “yes” ambiguity rejection, explicit confirm/cancel language, replay, peer-authored text rejection, and source generation fencing.

**Step 2: Add controls**

```python
@dataclass(frozen=True)
class PeerConfirmationControl:
    action: Literal["confirm_peer_action", "cancel_peer_action"]
    pending_id: str
    user_intent_excerpt: str
```

Hydrate a bounded pending-confirmation block only into the affected owner’s interactive Telegram turn. Apply controls immediately like user memory governance, but call the broker through a capability-scoped client.

**Step 3: Run GREEN**

Expected: direct owner confirmation works; foreign and ambiguous text cannot approve.

### Task 16: Deliver deterministic confirmation notices

**Objective:** Ensure a recipient knows approval is pending without letting a peer write arbitrary Telegram text.

**Files:**
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Modify: `tomo_core/src/tomo_core/peer_service.py`
- Modify: `tomo_core/src/tomo_core/onboarding_store.py`
- Create: `tomo_core/tests/test_peer_notifications.py`

**Step 1: Write RED tests**

Assert fixed templates, target installation binding, no request body leakage beyond bounded preview, deduplicated delivery, retry-safe receipt, and no notice after revoke/expiry.

**Step 2: Implement host-owned notices**

Example:

```text
bob’s tomo wants permission to share your availability for friday. say “confirm peer request <short-id>” or “cancel peer request <short-id>”.
```

The peer cannot choose the notice wording. Store delivery state before/after Telegram I/O, using the existing uncertain-delivery discipline.

**Step 3: Run GREEN**

Expected: notice tests pass.

### Task 17: Add authenticated dashboard relationship management

**Objective:** Give both owners inspectable, revocable control over handles, invitations, and directional grants.

**Files:**
- Create: `dashboard/src/lib/peer-client.ts`
- Create: `dashboard/src/app/api/peers/route.ts`
- Create: `dashboard/src/app/api/peers/[relationshipId]/route.ts`
- Create: `dashboard/src/app/tomos/page.tsx`
- Create: `dashboard/src/components/tomo-relationships.tsx`
- Modify: `dashboard/src/app/page.tsx`
- Create: `dashboard/src/lib/peer-client.test.ts`
- Read before implementation: `dashboard/AGENTS.md` and relevant Next 16 route-handler docs under `dashboard/node_modules/next/dist/docs/`

**Step 1: Write RED Bun tests**

Test Better Auth session requirement, forwarding only `session.user.id`, no control key in client bundles, safe upstream failures, and grant revision conflicts.

**Step 2: Implement BFF routes**

Reuse the authenticated server-side pattern in `dashboard/src/app/api/onboarding/telegram/route.ts`. Never accept a browser-supplied owner id.

**Step 3: Implement minimal UI**

Show exact handle, pending invites, active relationships, directional toggles for ordinary messages/auto-reply/availability, expiry, revoke, block, and thread audit. Avoid fake controls for future action grants.

**Step 4: Run GREEN**

```bash
cd dashboard
bun test
bun run lint
bun run build
```

Expected: tests, lint, and production build pass.

### Task 18: Add privacy, retention, and abuse controls

**Objective:** Make leakage, spam, and deletion semantics explicit and testable.

**Files:**
- Modify: `tomo_core/src/tomo_core/peer_exchange.py`
- Modify: `tomo_core/src/tomo_core/peer_store.py`
- Create: `tomo_core/src/tomo_core/peer_safety.py`
- Create: `tomo_core/tests/test_peer_safety.py`
- Modify: `tomo_core/src/tomo_core/personal_data_transfer.py`
- Modify: `tomo_core/tests/test_personal_data_transfer.py`

**Step 1: Write RED tests**

Cover secret/token patterns, message and thread caps, hourly quotas, handle enumeration resistance, block precedence, 30-day prune, owner export, confirmed purge, and the fact that sender deletion cannot retract recipient-disclosed content.

**Step 2: Implement**

Use fixed safe rejection codes. Logs and metrics may contain ids/hashes/statuses but never peer message bodies, pending previews, owner memory, credentials, or model text.

**Step 3: Run GREEN**

Expected: privacy and transfer tests pass.

### Task 19: Add complete inter-owner scenarios

**Objective:** Prove the architecture at the public seams rather than through implementation details.

**Files:**
- Create: `tomo_core/tests/test_peer_exchange_scenarios.py`
- Modify: `tomo_core/tests/test_conversation_scenarios.py`

**Step 1: Implement scenario tests**

Required scenarios:

1. Alice and Bob accept; Alice asks ordinary question; Bob auto-replies; Alice receives observation.
2. Alice asks availability; Bob’s grant allows it; no confirmation.
3. Alice asks exact calendar detail; Bob gets pending confirmation; no target execution before confirmation.
4. Bob explicitly confirms; one request executes once.
5. Bob says only “yes”; confirmation remains pending.
6. Relationship revoked while request leased; target execution is suppressed.
7. Peer prompt injection asks for tools/secrets; target has no mutating/peer tools and response is safely rejected or bounded.
8. Alice sends a new Telegram message, superseding her source generation before broker acceptance; peer request is not created.
9. Duplicate HTTP/tool retry returns the original request/result.
10. Owner A cannot inspect B–C relationships, threads, or pending actions.

**Step 2: Run focused suite**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests -p 'test_peer_*.py'
```

Expected: all peer tests pass.

### Task 20: Document, verify, and release safely

**Objective:** Verify the exact integrated candidate and preserve unrelated work.

**Files:**
- Create: `tomo_core/docs/inter-owner-tomo-exchange.md`
- Modify: `tomo_core/docs/conversation-architecture.md`
- Verify unchanged: `tomo_core/SOUL.md`

**Step 1: Document**

Include lifecycle, permission matrix, non-transitive authority, retention, revocation/non-retraction, central broker data, sandbox restrictions, failure behavior, and operator configuration.

**Step 2: Run full checks from an isolated committed/staged tree**

```bash
cd tomo_core
PYTHONDONTWRITEBYTECODE=1 uv sync --frozen
PYTHONDONTWRITEBYTECODE=1 uv run python -m unittest discover -s tests
uv build --wheel
cd ../dashboard
bun test
bun run lint
bun run build
```

Expected: all Python and dashboard checks pass; wheel contains new peer modules; no `.pyc`, auth DB, token, or generated dashboard artifact is staged.

**Step 3: Verify invariants**

```bash
git diff --check
git diff -- tomo_core/SOUL.md
```

Expected: clean diff check and **no SOUL changes**.

**Step 4: Hosted smoke sequence**

1. Create immutable Daytona snapshot from the exact integrated tree.
2. Activate `TOMO_DAYTONA_SNAPSHOT` with deployment skipped.
3. Push once and observe exactly one Railway core deployment.
4. Create two dedicated test Better Auth owners and Telegram installations.
5. Establish relationship and directional availability grant.
6. Run one allowed ask and verify one target PeerTurn, one response, no Telegram impersonation, no recursive peer tool.
7. Run one sensitive ask and verify pending confirmation with zero target executions.
8. Revoke relationship and verify immediate denial.
9. Inspect logs for safe codes only and verify no peer content or credentials appear.

## 4. Files likely to change

### Core additions

- `tomo_core/src/tomo_core/peer_models.py`
- `tomo_core/src/tomo_core/peer_policy.py`
- `tomo_core/src/tomo_core/peer_store.py`
- `tomo_core/src/tomo_core/peer_exchange.py`
- `tomo_core/src/tomo_core/peer_capability.py`
- `tomo_core/src/tomo_core/peer_api.py`
- `tomo_core/src/tomo_core/peer_tools.py`
- `tomo_core/src/tomo_core/peer_service.py`
- `tomo_core/src/tomo_core/peer_governance.py`
- `tomo_core/src/tomo_core/peer_safety.py`

### Core modifications

- `tomo_core/src/tomo_core/models.py`
- `tomo_core/src/tomo_core/control_api.py`
- `tomo_core/src/tomo_core/sandbox_dispatch.py`
- `tomo_core/src/tomo_core/sandbox_inbound.py`
- `tomo_core/src/tomo_core/sandbox_protocol.py`
- `tomo_core/src/tomo_core/runtime.py`
- `tomo_core/src/tomo_core/shared_gateway.py`
- `tomo_core/src/tomo_core/onboarding_store.py`
- `tomo_core/src/tomo_core/conversation/models.py`
- `tomo_core/src/tomo_core/conversation/parsing.py`
- `tomo_core/src/tomo_core/conversation/prompts.py`
- `tomo_core/src/tomo_core/cli.py`
- `tomo_core/src/tomo_core/personal_data_transfer.py`
- `tomo_core/CONTEXT.md`
- `tomo_core/docs/conversation-architecture.md`

### Dashboard

- `dashboard/src/lib/peer-client.ts`
- `dashboard/src/app/api/peers/route.ts`
- `dashboard/src/app/api/peers/[relationshipId]/route.ts`
- `dashboard/src/app/tomos/page.tsx`
- `dashboard/src/components/tomo-relationships.tsx`

## 5. Risks and tradeoffs

1. **Semantic disclosure enforcement:** deterministic policy can control categories and tools, but a model may still include an unrelated private fact in ordinary text. V1 mitigates this with narrow grants, explicit peer prompt framing, no raw tool observations, secret scanning, bounded output, audit, and confirmation for sensitive categories. Stronger information-flow control would require structured domain-specific exchanges rather than generic prose.
2. **External side-effect race:** once a peer response is accepted, supersession cannot retract it. Pre-dispatch generation checks, idempotency, and durable provenance limit duplicates but do not promise impossible retraction.
3. **Central trust:** same-control-plane brokering simplifies identity and signing but is not federation. Cross-deployment public-key identity, transport encryption, discovery, and abuse handling are separate work.
4. **Latency:** a synchronous tool observation now includes target sandbox reconciliation and model inference. Durable polling and a 60-second deadline must degrade to `pending` rather than blocking the entire Telegram turn indefinitely.
5. **Owner availability:** a target without an installation or healthy sandbox cannot auto-reply. Return a safe unavailable status and retain retry only while the source generation/thread remains valid.
6. **Privacy deletion:** sender-side deletion cannot erase information already delivered into another owner’s context. UI and docs must state this before granting communication.
7. **Approval fatigue:** v1 intentionally avoids standing grants for commitments. Add those only after real usage demonstrates repeated safe patterns.

## 6. Explicitly parked questions

- Independent Tomo deployments and public-key federation.
- Group relationships involving more than two owners.
- Attachments in peer messages.
- Recursive delegation or peer-to-peer tool calls.
- Standing grants for booking, purchasing, sending, or other commitments.
- Automatic import of peer claims into long-term memory.
- Third-party introductions and transitive sharing.
- Cryptographic end-to-end encryption beyond platform/volume encryption.

## 7. Planning completion checklist

- [x] Different owners, not same-owner multi-profile, is the product boundary.
- [x] Permission versus per-action confirmation is explicit.
- [x] Runtime participation and authority are traced end to end.
- [x] Tool binding covers hosted and local paths.
- [x] Fencing, retries, idempotency, revocation, and non-retraction are explicit.
- [x] Personal stores remain isolated; only disclosed exchange content enters the broker.
- [x] Dashboard/BFF and Telegram confirmation surfaces are included.
- [x] No SOUL fork or session picker is introduced.
- [x] TDD targets and full verification commands are exact.
