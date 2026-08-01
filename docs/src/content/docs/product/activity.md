---
title: Activity
description: The append-only audit ledger — the AuditEvent schema, which code paths write events, query fingerprinting, and the console view over it.
---

**Activity** is the administrator-only console view of the audit ledger: an
append-only record of API and MCP access to the index.

## What is recorded

Each entry is one `AuditEvent` row (`audit_events` table):

| Field | Type | Content |
| --- | --- | --- |
| `id` | string (UUID) | Primary key |
| `actor_principals` | JSON list | The caller's resolved principals; empty for unauthenticated requests |
| `action` | string | Dotted action name, e.g. `api.post.search`, `mcp.search_semantic`, `quarantine.retry` |
| `target_type` | string or null | `api_route`, `document`, `source_object`, `model`, `identity_provider`, `identity_user`, … |
| `target_id` | string or null | The route path, document id, alias, etc. |
| `outcome` | string | `success`, `denied`, or `error` |
| `details` | JSON | Per-writer payload (see below) |
| `created_at` | timestamp | Server-side default; indexed, as is `(action, outcome)` |

### Writers

| Path | Action | Details recorded |
| --- | --- | --- |
| HTTP middleware, every `/api/*` request | `api.<method>.<path>` (e.g. `api.get.status`) | `status_code` and `duration_ms`; outcome is `success` below 400, `denied` for 401/403, `error` otherwise or on exception. Written in a `finally` block, so a crashing handler is still recorded |
| Every MCP tool call (`audited_call`) | `mcp.<tool>` | Tool-specific: result counts, active filters, `found`, sizes, node ids. A failed identity resolution writes `denied` with empty principals; a tool exception writes `error` with the exception class. The write is not skippable — a call that cannot be ledgered fails |
| Rejected MCP bearer token (transport middleware) | `mcp.authenticate` | `denied`, with the rejection reason. The tokenless first step of the OAuth handshake is deliberately not recorded |
| Original-document downloads (`GET /api/downloads/…`) | recorded by the HTTP middleware | Attributed to the principals frozen into the download capability when the MCP tool issued it, not to whatever session fetched the link |
| Quarantine release (`POST /api/quarantine/{id}/retry`) | `quarantine.retry` | The stage, invalidated downstream stages, and the previous error that was overruled |
| Model registration (`POST /api/models/catalog`) | `models.register` | Upstream model, credential *name* (never a key), API base, mode |
| Identity administration | `identity.provider.configure` / `remove` / `test`, `identity.person.create` / `reset_password` / `enabled` / `disabled` / `delete` | Stage and reason on failure; no secret material |

Because MCP calls are served under `/mcp` (not `/api/*`), an MCP tool
invocation produces exactly one event — the `mcp.<tool>` row — while a console
action produces one `api.*` row per request.

### Query fingerprinting

Content-bearing query text is not stored. The tools that search privileged
content — `search_semantic`, `search_decisions`, `resolve_entity` — record a
`query_sha256` digest plus `query_chars` instead of the query itself, which
still answers "did the same query recur" without persisting what was asked.
One exception is deliberate and verifiable in code: `ontology_search` records
its query verbatim, because it searches the document-type vocabulary, not firm
content. The REST middleware records only method, path, status, and duration —
request bodies are never written to the ledger.

### Append-only enforcement

There is no update or delete path: the only operations against
`audit_events` anywhere in the application are inserts and ordered reads.
`GET /api/audit` is read-only, no endpoint mutates an existing row, and
nothing prunes the table (see Retention below). Append-only is a property of
the API surface, not a database trigger — direct database access is outside
this guarantee.

## The console view

The page requires an administrator; members see a note instead. It loads
`GET /api/audit?limit=150` — the endpoint returns newest-first rows, default
50, capped at 200 per request — and computes four headline metrics in the
browser over the fetched window:

| Metric | Computation |
| --- | --- |
| Recorded events | Rows returned (the window, not the table size) |
| Successful | Rows with outcome `success` |
| Denied / errors | Rows with outcome `denied` and `error`, respectively |
| Average API time | Mean of `details.duration_ms` across rows that carry one — i.e. HTTP-middleware events; MCP tool events record no duration and do not enter the average |

The list itself shows, per event: an icon keyed off the action name, the
humanized action with its outcome badge, the actor principals (or
"Unauthenticated request"), the target, relative time, and duration where
present. There are no server-side filters on this page; **Refresh** re-fetches
the window.

### Service links and traces

With the **Service links** toggle enabled (the topbar switch, persisted in the
browser), the page shows an **Open traces** button linking to the trace UI —
the `ui_url` of the "Traces" component (Langfuse) reported by
`GET /api/components`. That is where the full prompt/response traces of
model-backed pipeline and retrieval calls live; the ledger itself stores no
model inputs or outputs. With the toggle off, the console keeps deep links to
component dashboards hidden.

## Retention and limits

There is no retention or pruning job: audit rows accumulate for the life of
the database and are covered by [backups](/product/backup/). The only limits
in code are read-side — 200 rows per `GET /api/audit` request — plus one
internal consumer: the sign-in people list derives each person's "last seen"
timestamp by scanning the most recent 2,000 audit events for their principal.
