# Web-search architecture for Tomo

*Research date: 2026-07-14. This is an implementation design note, not an accepted ADR.*

## Decision summary

Add web search as a **read-only, owner-neutral evidence capability** with two
separate tools:

1. `web_search(query, freshness?, allowed_domains?)` discovers ranked source
   records; and
2. `web_open(source_id)` retrieves and extracts one previously discovered
   public source.

Do not expose an arbitrary-URL fetch tool in the model's initial tool set.
Search results must become typed evidence records with immutable provenance;
page text is untrusted data, never instructions. The agent may issue a
follow-up generation only after observations arrive, consistent with the
existing knowledge-boundary rule in `docs/conversation-architecture.md`.

## Why this shape

Hosted search products expose a search action separately from a page-open
operation, and return source/citation metadata with the response. OpenAI's
Responses web-search output, for example, contains `web_search_call` items
(`search`, `open_page`, and `find_in_page`) plus URL-citation annotations.
That maps cleanly to an explicit Tomo discovery-then-open boundary rather than
an opaque blob of scraped text. [OpenAI web-search guide][openai-web]

OpenAI and Anthropic both support domain controls for web search; use those as
an optional policy input, not as a claim that a domain is trustworthy.
[OpenAI web-search guide][openai-web] [Anthropic web-search tool][anthropic-web]

## Proposed capability contract

### Tool schemas

`web_search`

```text
query: string (1..500 chars)
freshness: "any" | "day" | "week" | "month" | "year" (default "any")
allowed_domains: string[] (0..20, optional; policy-intersected)
max_results: integer (1..8, default 5)
```

It returns an observation shaped like:

```json
{
  "ok": true,
  "search_id": "opaque-id",
  "results": [
    {
      "source_id": "opaque-id",
      "url": "https://example.test/article",
      "canonical_url": "https://example.test/article",
      "title": "…",
      "published_at": "2026-07-14T00:00:00Z",
      "snippet": "…",
      "rank": 1,
      "retrieved_at": "2026-07-14T00:00:00Z",
      "provider": "…"
    }
  ]
}
```

`web_open`

```text
source_id: opaque ID returned by web_search in the current turn
```

It returns a bounded extracted-text observation with the same canonical URL,
HTTP/content metadata, retrieval timestamp, content hash, truncation state,
and a stable citation ID. Reject IDs that were not produced by the current
owner/turn; never take a URL argument from the model.

This matches Tomo's existing design: tool parameters are JSON-compatible and
immutable in `src/tomo_core/tools.py`; the executor already rejects unresolved
references, enforces a per-turn call budget, executes compatible read-only
calls concurrently, and checks cancellation before and after execution
(`src/tomo_core/tool_execution.py`).

## Execution path

```text
model chooses web_search
  -> validate query and policy
  -> provider search (or configured search backend)
  -> normalize + persist short-lived evidence index
  -> untrusted, bounded observation to the next segment
  -> optional model-chosen web_open(source_id)
  -> safe fetch/extract + evidence record
  -> final frame renders only evidence-backed claims with citations
```

Keep the **search backend** and the **fetch/extraction backend** behind
separate interfaces. A hosted provider integration may offer both at once, but
the internal result contract should not depend on provider-specific citation
objects. For a first implementation, prefer a hosted search tool with native
citations over operating a general crawler. OpenAI documents both a cache-only
mode and live external access, so the product can expose a deliberate
freshness/privacy setting rather than silently selecting one. [OpenAI
web-search guide][openai-web]

### Tool-round rules

- `web_search` calls whose arguments are fully resolved may run together with
  other independent read-only tools. `web_open` is dependent on a particular
  search result and therefore belongs in a later tool round.
- Cap the normal conversational path at one search and at most two opens;
  reserve multi-query loops for an explicit research workflow or background
  job. This preserves the current one-to-three-bubble latency envelope.
- Cache search responses briefly by normalized query + policy + freshness;
  cache extracted documents by canonical URL + validator/content hash. Cache
  hits retain their original retrieval timestamp. For document caching, honor
  HTTP `Cache-Control`, `ETag`, `Last-Modified`, and `Vary`, then use
  conditional revalidation rather than an invented TTL. [RFC 9111][rfc9111]
- Send provider requests with per-owner and global rate/concurrency limits,
  timeouts, size limits, and retry only idempotent failures. Honour a server's
  `Retry-After` guidance when present. A `429` response must not be cached.
  [RFC 6585 §4][rfc6585] [RFC 9110 §10.2.3][rfc9110]

## Evidence, citations, and retention

A final answer should cite the smallest source set supporting its factual
claims. Citation rendering should use `title`, canonical URL, and retrieval
or publication time where useful; do not cite a search-result snippet as if it
were the page. Store the evidence ledger separately from personal memory:

- `turn_id`, `tool_call_id`, provider, query/source ID, URL and canonical URL;
- retrieval time, content type, HTTP validator/status when available;
- content hash, extraction version, character cap/truncation flag;
- cited spans or source IDs used in the final frame.

This creates replayable provenance without treating remote pages as durable
personal data. It also preserves Tomo's rule that tool observations stay
internal unless a later validated frame naturally communicates a verified
result (`docs/conversation-architecture.md`).

OpenAI requires web-derived citations to be clearly visible and clickable when
shown to end users. Treat that as a baseline UI requirement for Telegram and
future surfaces. [OpenAI web-search guide][openai-web]

## Security boundary

1. **Prompt injection:** Render every snippet and extracted page as untrusted
   tool data with a clear origin label. Never allow page content to change
   tool policy, reveal system/developer instructions, or trigger a tool call
   without the model independently choosing a schema-valid call. OWASP's AI
   Agent guidance identifies indirect prompt injection as a core agent risk.
   [OWASP AI Agent Security][owasp-agent]
2. **SSRF:** Do not let `web_open` accept arbitrary URLs. The fetch service
   must allow only `http`/`https`, resolve and re-check every redirect target,
   and deny loopback, link-local, RFC1918/ULA, multicast, and provider
   metadata address ranges after DNS resolution. Run it with no access to
   Tomo's internal network or credentials. OWASP recommends allow-listing
   trusted destinations where possible and explicitly calls out URL/address
   validation and DNS-rebinding concerns. [OWASP SSRF Prevention][owasp-ssrf]
3. **Resource abuse:** enforce redirect, response-byte, decompressed-byte,
   page-count, content-type, parse-time, and total-tool-budget ceilings;
   reject non-text content unless a dedicated parser is explicitly enabled.
4. **Crawling etiquette:** if Tomo performs direct retrieval rather than using
   a provider's index, fetch and cache `/robots.txt` per origin and honour the
   applicable rules. The Robots Exclusion Protocol is a crawler convention,
   **not authorization**, so it cannot be the security control. [RFC 9309
   §1][rfc9309]
5. **Privacy:** do not place personal-memory text, credentials, user IDs, or
   private conversation context into a search query by default. Build search
   queries from the user request and explicit model-selected terms only; log
   redacted operational metadata rather than page bodies.

For a direct-fetch implementation, persist raw bytes and headers before
extraction, record a SHA-256 content hash and extractor version, and parse
only from that stored snapshot. Use normal HTTP retrieval for HTML/PDF;
headless rendering, if ever needed for JavaScript-only pages, needs its own
isolated environment with downloads disabled and no authenticated session.

## Fit with the current codebase

`PersonalAgentRuntime` currently constructs mandatory owner-bound personal
search tools and extends them with caller tools only when the provider supports
tool calls (`src/tomo_core/runtime.py:77-108`). Add a
`web_search_registry(config, evidence_store, owner_id)` beside
`personal_search_registry`, then extend it at runtime construction. Keep the
web evidence service outside `PersonalDataRepository`: personal search is
owner-scoped private data, whereas web evidence is externally retrieved,
short-lived, and must carry URL/content provenance.

The initial contract should set `read_only=True`, `parallel_safe=True`, and
`internal_context=True` on both web tools. Add contract tests for schema
bounds, source-ID scoping, redirect/IP denial, content caps, provenance fields,
citation rendering, cancellation, and failure isolation.

## Delivery plan

1. Define typed `WebSource`, `WebEvidence`, and a small `WebSearchBackend`
   protocol; write unit tests for normalization and policy enforcement.
2. Implement search-only with a hosted backend and return source records.
3. Add stateful `web_open(source_id)` with a hardened isolated fetcher.
4. Add final-answer citation metadata to the frame/delivery contract, then
   render visible links in Telegram.
5. Measure query count, cache hit rate, tool latency, citation coverage, open
   failures, SSRF/policy blocks, and answer-without-evidence rate. Red-team
   indirect prompt injection before enabling broad live access.

## Primary sources

- [OpenAI, *Web search*][openai-web]
- [OpenAI, *Function calling*][openai-function-calling]
- [Anthropic, *Web search tool*][anthropic-web]
- [OWASP Cheat Sheet Series, *AI Agent Security*][owasp-agent]
- [OWASP Cheat Sheet Series, *Server-Side Request Forgery Prevention*][owasp-ssrf]
- [IETF, *RFC 9309: Robots Exclusion Protocol*][rfc9309]
- [IETF, *RFC 9111: HTTP Caching*][rfc9111]
- [IETF, *RFC 6585: Additional HTTP Status Codes*, §4][rfc6585]
- [IETF, *RFC 9110: HTTP Semantics*, §10.2.3][rfc9110]

[openai-web]: https://developers.openai.com/api/docs/guides/tools-web-search
[openai-function-calling]: https://developers.openai.com/api/docs/guides/function-calling
[anthropic-web]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
[owasp-agent]: https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html
[owasp-ssrf]: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
[rfc9309]: https://www.rfc-editor.org/rfc/rfc9309.html
[rfc9111]: https://www.rfc-editor.org/rfc/rfc9111.html
[rfc6585]: https://www.rfc-editor.org/rfc/rfc6585.html#section-4
[rfc9110]: https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3
