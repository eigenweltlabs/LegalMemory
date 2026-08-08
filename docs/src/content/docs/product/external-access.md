---
title: External access
description: The MCP endpoint and its registered tools, the OAuth resource-server surface, the REST search API, the OpenAPI schema, and the external-clients registry.
---

**External access** is how other tools reach LegalMemory. The console page
shows three endpoint cards, the live MCP tool list, a copy-pasteable client
configuration, and (for administrators) the external-clients registry.

| Endpoint | What it is |
| --- | --- |
| `/mcp/` | The MCP server: streamable-HTTP transport, stateless, JSON responses. |
| `POST /api/search` | REST hybrid search for integrations that do not speak MCP. |
| `/openapi.json` | The generated API schema; interactive docs at `/docs`. |

## The MCP endpoint

The MCP server is built on FastMCP and mounted at `/mcp` on the same FastAPI
app as the console (`http_app(path="/", stateless_http=True,
json_response=True)`). Its server instructions direct a connected model to
treat the index as the primary source for firm-document questions and to never
make a factual claim from a result without a non-empty `citations` array.

### Identity binding

Every request to `/mcp` (except CORS preflights) passes a middleware that
resolves the caller before the JSON-RPC layer answers:

- A presented `Authorization: Bearer` token is always the answer, valid or
  not: it is verified against the identity provider's JWKS with an audience
  check tying it to this appliance's resource identifier (RFC 8707). A
  rejected token never falls back to anything else.
- Without a token, the request is refused unless
  `security.mcp_allow_trusted_header` is enabled (off by default; intended for
  a trusted reverse proxy, never an open network).

The validated identity's principals are the only ACL input a tool ever sees:
inside each tool, `principals_from_headers(headers, config)` calls
`resolve_mcp_identity` and passes the resulting principal set to the retrieval
layer. There is no request parameter, header fallback, or tool argument
through which a caller can name its own principals; the older optional-config
form that trusted an `x-ki-principals` header outright was removed for exactly
that reason. The same invariant holds on REST: `/api/search` derives
principals from the request's resolved identity and its body schema has no
principals field.

### Tools

The list below is what `create_mcp_server` registers, in registration order.
The console never hard-codes this list; it reads `GET /api/mcp/tools`
(administrator only), which enumerates `mcp.list_tools()` live with each
tool's name, short title, and tags.

| Tool | What it does | Notable parameters |
| --- | --- | --- |
| `search_filter` | Lists documents by exact metadata filters, no query text. | `project_id`, `matter_id`, `doc_type`, `version_status`, `language`, `date_from`/`date_to`, `clause_type`, `limit` (default 20, capped 100), `offset` |
| `search_semantic` | Hybrid semantic + lexical search over chunks, ACL-filtered before ranking. | `query`, the same metadata filters, `limit` (default 8, capped 100), `offset` (see the ranked-window cap below) |
| `get_document` | Reads one authorized document version as paginated text. | `document_id`, `version_id`, `offset`, `max_chars` (1–50,000; default 30,000), `include_structured_metadata`; returns a `content_page` cursor with `next_offset`/`has_more` |
| `download_document` | Exports the exact original binary via a short-lived download link (see below). | `document_id`, `version_id`, `source_object_id`, `inline_blob` (embeds the base64 blob instead of only linking) |
| `find_related_documents` | Stored relations plus labeled shared-thread and shared-matter context, with graph-ready edges. | `document_id`, `include_same_matter` (default true), `limit` (capped 250), `offset`; `page.total` is exact and the edge lists cover the current page |
| `traverse` | Low-level walk of stored relation edges (`supersedes`, `annex_of`, `references`, `responds_to`, `belongs_to_thread`). | `entity_type`, `entity_id`, `limit` (capped 250), `offset`; the limit counts *visible* edges |
| `list_matters` | Matters containing at least one version visible to the caller. | `limit` (capped 250), `offset`, `practice_area` (Area-of-Law node id, subtree semantics); the limit counts *visible* matters, and no `total` is reported |
| `billing_rollup` | Invoiced total plus hours/fees per UTBMS task code for one matter. | `matter_id`; fails closed if any invoice lacks exact provenance. Not paginated — the rollup covers every invoice |
| `list_invoices` | A matter's invoices (number, date, total) with per-invoice citations. | `matter_id`, `limit` (capped 250), `offset`; `page.total` is the matter's exact invoice count |
| `resolve_entity` | Resolves a party/client name or identifier (LEI, HRB, VAT) to known entities. | `query`, `limit` (capped 100), `offset`; results without an authorized citation are withheld, so a short page can still have `has_more` |
| `search_decisions` | Searches anonymized drafting and negotiation rationale. | `query`, `limit` (capped 100), `offset`; `page.total` is exact |
| `list_taxonomies` | The active document-type ontology scope plus task types and practice areas. | none. Not paginated — returns the roots and the top two practice-area levels, with `ontology.visible_nodes` as the true node count |
| `ontology_search` | Finds document-type ontology nodes by name, synonym, or definition. | `query`, `limit` (default 12, capped 100), `offset`; `page.total` is exact |
| `ontology_roots` | Top-level branches of the active document-type ontology. | none. Not paginated — returns every root, with `complete: true` |
| `ontology_children` | Children of one ontology node, one level at a time. | `node_id`, `limit` (capped 250), `offset`; `page.total` is the node's exact child count |
| `ontology_node` | Full detail for one node: definition, synonyms, path, parents. | `node_id` |
| `preview_search_scope` | Compiles the caller's ACL (plus optional selections) into the exact scope that constrains retrieval before scoring. | `project_ids`, `document_ids`, `limit` (capped 500), `offset`; tagged `scope` rather than `read`. `project_count`/`document_count` are always the whole scope; the enumerated documents are paged |

Search-shaped tools accept a `doc_type` that matches the named ontology node
**and its whole subtree**; `clause_type` additionally narrows the search to
clause chunks.

### Pagination

Every list-shaped tool returns `{results, page}` rather than a bare list, and
says so in its own description as well as in the server instructions. A bare
list cannot distinguish "that is everything" from "that is the first 20 of
1,300", so a caller holding exactly `limit` rows had no way to tell whether the
corpus continued — it either stopped early or re-issued the same call with a
larger limit until it hit the cap.

```json
{
  "results": [ … ],
  "page": {
    "offset": 0, "limit": 20, "returned": 20,
    "has_more": true, "next_offset": 20,
    "total": 137
  }
}
```

- `has_more` is always exact. The next page is the same call with
  `offset = page.next_offset`; `next_offset` is `null` on the last page.
- `total` appears **only where it can be counted without doing the work of
  every page** — a SQL count (`list_invoices`), or a set the tool already
  materialized in full (`search_decisions`, `find_related_documents`,
  `ontology_search`, `ontology_children`). It is absent from the index-backed
  searches and from `list_matters`, where counting means authorizing every
  candidate in the estate. Branch on `has_more`, not on the row count.
- **The limit counts rows the caller receives.** `list_matters` and `traverse`
  filter by access control *before* applying it. Previously both applied a SQL
  `LIMIT` first and dropped unauthorized rows afterwards, so a caller asking for
  100 matters got however many of the first 100 titles it happened to be allowed
  to see — and nothing alphabetically later was reachable at any limit.
- **A limit above the cap is clamped, not rejected**, and the effective value is
  echoed in `page.limit`. A negative `offset` or a `limit` below 1 is refused
  outright.
- **The ranked-window cap.** `search_semantic` and `search_filter` re-rank the
  whole window on every call, so `offset + limit` must stay within 500; past
  that they raise rather than clamp, pointing the caller at `matter_id`,
  `doc_type`, `party`, or a date range. Ranked pages are stable only while the
  index is unchanged — this is re-ranking, not a cursor.
- `get_document` paginates by **character**, not by row, and keeps its own
  `content_page` block (`offset`, `returned_chars`, `total_chars`, `has_more`,
  `next_offset`).
- Tools whose description says **NOT PAGINATED** return their complete result in
  one call and may be treated as a total.

### Citations

Every evidence-bearing result carries a `citations` array naming the exact
project, document, version, and source objects behind it. Tools enforce this
rather than merely promising it: `billing_rollup` and `list_invoices` raise
instead of answering when an invoice's source provenance is missing, and
`resolve_entity` silently drops entities the caller holds no citation for.

### The access-ledger write per call

Every tool invocation runs inside `audited_call`, a context manager that
writes one `AuditEvent` row per call: action `mcp.<tool>` (for example
`mcp.search_semantic`), the caller's principals, the target where one exists
(`document`/`entity` id), and an outcome of `success`, `error`, or `denied`.
Identity-resolution failures are recorded as `denied` with empty principals
before the error propagates; the ledger write is not skippable. Details vary
per tool (result counts, active filters, `found`, sizes); content-search query
text is stored as a SHA-256 fingerprint plus character count, never verbatim
(see [Activity](/product/activity/)).

### Downloads

`download_document` never puts document bytes in model context by default.
It issues a process-local capability token (`secrets.token_urlsafe(32)`,
TTL 300 seconds) that freezes the document/version/source-object identity,
content hash, and, critically, the caller's principals at issuance. The
returned `ResourceLink` points at
`GET /api/downloads/{token}/{filename}`, alongside a ready-to-run `curl`
command, the SHA-256, size, and MIME type. On every fetch the endpoint
re-checks the ACL snapshot with the captured principals, so a revoked grant
invalidates an unexpired link (the token is also revoked on failure: 404 for
invalid/expired or no-longer-authorized, 410 for a missing blob). The fetch
itself lands in the audit ledger attributed to the capability's principals.

## The OAuth resource-server surface

LegalMemory never issues tokens; the firm's identity provider is the
authorization server and the appliance only verifies what it signed. Two
unauthenticated pieces let a stock MCP client sign a lawyer in with nothing
pasted:

- **The 401 challenge.** An unauthenticated request to `/mcp` gets `401` with
  `WWW-Authenticate: Bearer resource_metadata="…"` (RFC 6750 §3.1,
  RFC 9728 §5.1), the only signal that makes a client start a login rather
  than report a connection error. A request that *presented* a token which
  failed validation additionally gets `error="invalid_token"` with a
  description, and is recorded in the audit ledger as `mcp.authenticate` /
  `denied`; the tokenless first step of the handshake is not.
- **Protected-resource metadata.**
  `GET /.well-known/oauth-protected-resource/mcp` (and the bare
  `/.well-known/oauth-protected-resource`, for clients that treat the
  appliance root as the resource) serves the RFC 9728 document: the resource
  identifier, `authorization_servers`, and `scopes_supported`, with
  `Access-Control-Allow-Origin: *` so browser-hosted clients can read it.
  `GET /.well-known/oauth-authorization-server` answers clients that predate
  RFC 9728 with a 307 to the identity provider's own OpenID configuration.

The resource identifier defaults to the public base URL + `/mcp` and is
overridable; full identity-provider setup, scope configuration, and an
end-to-end verification transcript are in
[Deployment & identity](/operations/deployment/#signing-in-from-an-mcp-client).

## REST search: `POST /api/search`

Authentication is the console's own: the request's identity is resolved from
its headers (session or trusted proxy), and an unauthenticated request gets
401. Request body:

| Field | Type | Notes |
| --- | --- | --- |
| `query` | string, max 2000 chars | Empty string switches from hybrid search to a pure metadata listing |
| `project_id`, `matter_id`, `doc_type`, `version_status`, `language` | string or null | Metadata filters |
| `limit` | 1–100, default 20 | |

The response is `{scope, hits}`. `scope` reports the compiled ACL scope the
query ran under: `fingerprint`, project and document counts, and the active
filters. Each hit carries `document_id`, `project_id`, `version_id`,
`matter_id`, `title`, `doc_type` and `doc_type_label`, `version_status`,
`score`, a term-centered `excerpt`, `source_paths`, `matched_identifiers`, and
`citations`.

## OpenAPI and API docs

`/openapi.json` is the generated schema for the REST API (version 0.2.0), and
FastAPI's interactive documentation is served at `/docs`. Internal routes
(the download capability endpoint, the well-known documents, and the static
assets) are marked `include_in_schema=False` and do not appear there. With
the console's **Service links** toggle off, deep links to the API docs are
hidden from the everyday admin surface.

## External clients registry

Administrators register machine callers so they exist as named, auditable
principals:

| Endpoint | Auth | Behaviour |
| --- | --- | --- |
| `GET /api/external-clients` | admin | Lists registrations: `id`, `name`, `kind`, `status`, `principal`, `secret_ref`, `allowed_project_ids`, `last_used_at`. |
| `POST /api/external-clients` | admin | Creates an `ExternalClient` row with status `active`. Body: `name` (unique), `kind` (`mcp` or `api`), `principal`, optional `secret_ref`, `allowed_project_ids` (empty means every project the principal is granted). |

Registration creates a database row and nothing else: no token, no grant, no
access. `secret_ref` is a vault *reference*, never a secret value. The
registered principal becomes visible in the grant picker (`/api/principals`
reports it with origin `client`), and the client can read nothing until that
principal holds a project or document grant; the console's registration modal
says so out loud.
