---
title: Architecture
description: The system design — Postgres as the spine, the SyncSource contract, orchestrated sync runs, the insertion pipeline's robustness contract, and the MCP surface.
---

On-prem, air-gap-capable, Docker-Compose-deployable. One Postgres instance is the
spine: source-of-truth tables (ontology), pipeline state, and queues live there. Every
other service is stateless or rebuildable from Postgres + the artifact store.

```
┌────────────┐   ┌──────────────────────────────────────────┐   ┌─────────────┐
│ DMS/Storage │──▶│  Sync workers (SyncSource implementations)│──▶│             │
│ SharePoint  │   └──────────────────────────────────────────┘   │             │
│ iManage     │   ┌──────────────────────────────────────────┐   │  Postgres   │
│ SMB, IMAP   │   │  Pipeline workers (durable, per-doc)      │◀─▶│  + pgvector │
│ RA-MICRO …  │   │  fetch→convert→classify→relate→extract→…  │   │  (spine)    │
└────────────┘   └──────────────────────────────────────────┘   │             │
      ▲                    │            │                        └─────────────┘
      │ read-only          ▼            ▼                              ▲
      │            ┌─────────────┐ ┌──────────────┐            ┌──────┴──────┐
      │            │ Artifact FS │ │ Model gateway │            │ MCP server  │
      │            │ (derived    │ │ (LiteLLM →    │            │ + Admin UI  │
      │            │  data only) │ │  local/API)   │            └─────────────┘
      │            └─────────────┘ └──────────────┘
```

## 1. SyncSource interface

Connectors are read-only and dumb on purpose; all intelligence lives behind the
interface. An integrator implements exactly this per customer system:

```python
class SyncSource(Protocol):
    kind: str                                  # "sharepoint", "smb", ...
    capabilities: SourceCapabilities           # what this source can do

    def full_scan(self) -> Iterator[SourceObjectObservation]:
        """Enumerate everything. Resumable: yields in stable order,
        engine checkpoints periodically."""

    def changes(self, cursor: str | None) -> ChangeBatch:
        """Incremental sync since cursor. Returns observations + deletions
        + next_cursor. Sources without native deltas raise Unsupported;
        the engine falls back to periodic full_scan diffing."""

    def fetch(self, external_id: str) -> BinaryStream:
        """Stream content for one object. Must be safe to call repeatedly."""

    def acl(self, external_id: str) -> list[AccessGrant] | None:
        """None = source has no readable ACLs (engine applies source default)."""

@dataclass
class SourceCapabilities:
    delta: bool          # native change feed (Graph delta, iManage events)
    webhooks: bool       # push notifications
    acl: bool            # per-object ACLs readable
    versions: bool       # source has native version history
    stable_ids: bool     # ids survive rename/move (else path+hash heuristics)
```

The engine around it handles: scheduling (webhook > delta poll > full-scan diff),
checkpoint persistence, rate-limit/backoff, content-hash change detection (mtime/etag
are hints, the hash decides), tombstoning, and ACL snapshots. A new connector is
~200–400 lines against one system's API, testable against the mock DMS testbed.

### 1a. Sync is an orchestrated pipeline, not a request

Scanning an estate is minutes to hours of I/O against a system this appliance does not
control, so it is never done inside the HTTP request that asked for it. `POST
/api/actions/sync` **reserves** work and returns `202` immediately:

```json
{"runs":  [{"run_id": "…", "source_id": "…", "display_name": "Matters"}],
 "skipped":[{"source_id": "…", "display_name": "…", "reason": "a sync is already in flight …"}]}
```

An optional body `{"source_id": "…"}` syncs one source; no body means every syncable
source. Each reserved run is a `pipeline_runs` row with `workflow = "source-sync"`, so a
sync appears in `GET /api/runs` beside insertion runs and survives a closed tab.

- **Workflow** — `knowledge-index-source-sync`, two Hatchet tasks over one run row:
  `scan` (the `SyncEngine` pass, no retries: a failed scan is a revoked scope or an
  expired licence, and a retry only pays for another crawl) then `handoff`
  (retried — it is one cheap local enqueue).
- **Lifecycle** — `queued` → `running` (`current_step` carries the live observation
  count; `progress` stays 0 because a scan has no denominator until it ends, and a
  moving bar that measures nothing is a lie) → `completed` with `progress = 1`, or
  `failed` with the cause in `error` and the source set to `error`.
- **Counters** — `observed`, `created`, `changed`, `unchanged`, `restored`,
  `tombstoned`, `batches`, `mode`, `trigger` (`api` / `watch` / `event` / `cli`) and
  `insertion_run_id`.
- **No overlap** — enforced by the partial unique index `uq_pipeline_runs_active_sync`
  (one unfinished `source-sync` run per source) behind a per-source advisory lock, not
  by a disabled button. A second request puts the source in `skipped`.
- **One path** — the sync button, `ki sync`, the scheduler and the folder watcher all
  call `sync.runs.enqueue_sync`. Nothing scans on its own thread, so a scheduled sync and
  an operator click are the same kind of run and cannot collide.
- **Handoff** — a run that created, changed, restored or tombstoned anything starts the
  insertion pipeline and records its id as `insertion_run_id`. Controlled by
  `pipeline.auto_insert_after_sync`, **on by default**; turned off, the sync completes
  with a null `insertion_run_id` and nothing is converted or embedded until a partner
  says so.
- **In-process deployments** — with `components.orchestrator_provider = "local"` the run
  is reserved identically and executed on a background thread. Single-VM installs get
  the same run ledger, not a synchronous request.

Everything the engine guarantees runs inside `scan`: confirmed deletions, the
selection-fingerprint re-scope that applies them immediately, `security.acl_refresh_hours`
forcing a periodic full scan, and per-source failure isolation (one unreadable estate
fails its own run only).

### 1b. Scheduling: every continuous source, whatever its kind

`sync_policy = {"mode": "continuous", "interval": "2m"}` is honoured by
`sync/scheduler.py` for **every** source — SharePoint, Gmail, Slack and a mounted folder
alike. It ticks in the app process (started by `ki serve`, not by `create_app`, so
importing the app never starts crawling an estate), because the app is the only process
present in every deployment: there is no Hatchet worker under the in-process
orchestrator, and a firm with no mounted folders has no reason to run the watcher.
`KI_SYNC_SCHEDULE_SECONDS` caps the tick sleep; `0` leaves scheduling out entirely.

A source is due when it is not paused, not `pending_auth`, has no sync in flight, its
policy is continuous, and its most recent `source-sync` run started or finished at least
one interval ago. Due-ness is read from the run ledger rather than from
`sources.last_sync_at`, because a source whose scans fail never updates `last_sync_at`
and would be re-enqueued on every tick forever.

The folder watcher (`ki watch`) stays, and is now only about latency: a mounted folder
can *tell* the appliance a file changed, so an event enqueues a run within a second.
The interval belongs to the scheduler for every kind of source, so nothing is scheduled
twice.

The full crawl also resets and persists the provider cursor it establishes. Scheduled
Google Drive runs therefore consume Drive Changes and unscoped SharePoint runs consume
Graph delta after the initial crawl, rather than throwing away the checkpoint and
enumerating the whole estate on every interval. A periodic full scan still re-reads ACLs.
If that crawl finds identical bytes, metadata churn does not re-run document processing;
an ACL-only difference queues only the index access projection.

### 1c. Provider events are connector adapters, not another sync engine

`ConnectorSpec.event_adapter` is an optional `module:Class` reference. Its class
implements `ConnectorEventAdapter`: calculate the subscription targets implied by a
source's scope/cursor, then create, renew and delete those provider subscriptions. The
shared manager owns the rest:

```
provider subscription ──▶ outbound broker ──▶ transport consumer
                                                   │ provider subscription id
                                                   ▼
connector_event_subscriptions ──▶ enqueue source-sync(trigger=event)
                                                   │
                                                   ▼
                                      Drive Changes / Graph delta
                                                   │
                                                   ▼
                                         the normal SyncEngine diff
```

This makes the extension point reusable without weakening correctness:

- Google Drive plugs in Workspace Events + a Pub/Sub pull consumer.
- SharePoint plugs in Microsoft Graph subscriptions + an Azure Event Hubs consumer.
- A future connector supplies an adapter and transport normalizer, while scheduling,
  renewal, persisted error/status state, event coalescing, and source-run exclusion stay
  connector-neutral.
- Notifications contain no authoritative document state. They are only a prompt to read
  the provider's durable change feed.
- `connector_event_subscriptions` persists provider ids, targets, expiry, status and last
  event. `connector_event_checkpoints` persists broker partition offsets.
- The continuous policy interval remains a reconciliation boundary. Event delivery
  lowers latency; it never disables missed-event recovery or periodic full ACL refresh.

The provider-specific targeting matters. Workspace Events can cover a selected My Drive
folder or shared drive but not the whole My Drive root. Graph subscriptions cover
SharePoint document-library roots learned during scope selection/the first scan. The
connection API/UI reports `active`, `pending`, `unconfigured`, or
`reconciliation_only` instead of claiming coverage that the provider cannot deliver.

### 1d. A large deletion is confirmed, not refused

A scan that would tombstone more than `sync_policy.max_tombstone_fraction` (default 0.5,
above `MIN_OBJECTS_FOR_FRACTION_GUARD` objects), or that returns nothing at all, does not
delete and does not fail. It records the exact **set** of missing external ids in
`source_deletion_watches` / `source_deletion_candidates` and returns a normal result
carrying `pending_deletions` and the confirmation progress. The tombstones are applied
when `pipeline.deletion_confirmations` (default 3) consecutive scans report that
identical set; a set that differs, or objects that come back, discards the claim and
starts again from one. The set and not the count, because 340 missing today and a
different 340 tomorrow is a connector returning garbage.

The documents stay indexed and searchable while this happens, so the source payload
carries `pending_deletion` and the connections page says
"340 documents look deleted — confirming (2 of 3 syncs)". Immediate deletion still
applies where there is nothing to confirm: a connector with `verifiable_emptiness` (a
directory listing is the estate), a re-scope, or `sync_policy.allow_empty_scan`.

## 2. Insertion pipeline

Per-SourceObject state machine; stages are independent, idempotent, resumable.
Each stage reads its inputs from Postgres/artifacts and writes exactly one thing.
The table describes the stable product contract.

| # | Stage | In → Out | Model use |
|---|---|---|---|
| 1 | `fetch` | source bytes → Blob + content hash | none |
| 2 | `convert` | Blob → structured JSON artifact (text, layout, tables, tracked changes, comments, OCR w/ confidence), recursive container unpack (PST→mails→attachments, zip) | OCR/VLM, local |
| 3 | `classify_matter` | converted doc + practice-mgmt data → matter assignment (or new candidate matter) | small LLM |
| 4 | `relate` | corpus-local signals → Relations: dup/near-dup (hash/MinHash), version chains (filename signals + content similarity + tracked-changes lineage), annexes, email threads (JWZ), references | small LLM assist |
| 5 | `extract_metadata` | **final versions only** → doc_type, parties, dates, language, title-normalization | configurable LLM |
| 6 | `extract_decisions` | version deltas + surrounding thread → DecisionRecords (anonymized via PII pass) | configurable LLM |
| 7 | `gen_evals` | recognized completed tasks → EvalRecords with rubrics | configurable LLM, off by default |
| 8 | `index` | artifacts + metadata → chunking, embeddings, BM25/vector upsert with ACL + metadata payload | embedding model |

Stage-ordering nuances:
- 3, 4 iterate: matter assignment improves relation detection and vice versa. Both
  stages are re-runnable; `producer_version` bump = corpus-wide cheap re-pass.
- 5–7 subscribe to *knowledge-layer* conditions (e.g. "version chain has a final"),
  not just per-object readiness — the scheduler materializes these as derived queues.

### Robustness contract (the non-negotiables)

- **Idempotency**: every stage keyed by `(content_hash | object_id, stage,
  producer_version)`; re-execution is always safe.
- **Retries**: exponential backoff with jitter, `max_attempts` per stage class
  (transient IO vs deterministic parse failure detected via error taxonomy);
  deterministic failures skip straight to quarantine.
- **Quarantine, not blockage**: poison documents (2 GB scans, zip bombs, cursed
  encodings, password-protected files) land in `quarantined` with error class +
  sample bytes ref; corpus processing never stalls. UI shows quarantine counts per
  error class; bulk re-queue after a fix.
- **Waiting is not skipped**: a downstream stage is *stored* as `skipped` while its
  predecessor is unfinished, because `pending` would hand it to a worker that cannot
  run it. That storage detail never reaches an operator: `/api/status`, `ki status` and
  the per-run stage counts report those rows as **`waiting`** (see
  `taxonomies.stage_bucket`). A dashboard reading `skipped 499` at every stage says the
  pipeline looked at the corpus and declined it; the truth was that `fetch` had not
  finished. Completion ratios have always excluded blocked stages and still do — only
  genuinely skipped work (a disabled stage, a stage with nothing to do) counts as
  settled.
- **Timeouts + resource fences**: converters run in subprocess pools with hard
  wall-clock/memory caps (a hung LibreOffice or a pathological PDF kills one worker
  slot, not the pipeline).
- **Backpressure**: queue depth per stage drives sync-worker pacing; model-gateway
  stages have concurrency budgets so embedding/OCR can't starve extraction.
- **Crash-resume**: workers claim work via Postgres (`FOR UPDATE SKIP LOCKED` or the
  chosen queue's semantics); an abandoned claim times out back to `pending`.
- **Versioned everything**: prompts, models, converter versions are config; bumping
  one re-queues exactly the affected stage for affected objects.

## 3. Retrieval & MCP surface

Metadata-first, vectors second. MCP tools are deliberately separate so a smart agent
composes them:

| Tool | Behavior |
|---|---|
| `search_filter` | typed MVP filters (matter, doc_type, status, language, date range) → doc list; no embeddings involved. Client/task/party filters are schema-ready production additions |
| `search_semantic` | vector + BM25 hybrid over chunks, optional reranker; accepts the same filters as pre-filter |
| `get_document` | full structured content of a version + metadata + relations |
| `traverse` | walk Relations from a node (version chain, annexes, thread, references) |
| `list_matters` | matters with at least one version visible to the caller |
| `search_decisions` | query DecisionRecords (anonymized layer) |
| `billing_rollup` / `list_invoices` | matter billing with fail-closed invoice-source citations |
| `resolve_entity` | client/party resolution, restricted to entities backed by authorized matter documents |
| `preview_search_scope` | exact authorized project/document IDs and their citations |
| `list_taxonomies` | current doc/task/practice-area trees for the calling agent |

Every evidence-bearing row has a `citations` array. A citation identifies the exact
project, logical document, document version, source object, connector, and path; search
citations also identify the matched chunk and its originating source object. Tools with
structured evidence (billing, decisions, entity resolution) withhold rows when that
evidence cannot be resolved to an authorized source. Static taxonomies and empty/denied
results are non-evidentiary and therefore have no citations.

Every call carries the end-user identity (OAuth 2.1 / header from the embedding app);
ACL filtering happens before ranking or traversal results are built, not post-hoc.
Every tool invocation is audit-logged; query text is represented only by a fingerprint.

## 4. Configuration model (what the admin UI edits)

```yaml
ask_model: ...                                  # /api/ask planning + synthesis
pipeline:
  stages:                                       # each stage carries its own model
    classify_matter:   {model: ...}
    relate:            {model: ...}
    extract_metadata:  {model: ...}
    extract_decisions: {model: ..., enabled: true}
    gen_evals:         {model: ..., enabled: false}
  quarantine: {max_attempts: 5, max_file_mb: 512}
retrieval:
  embedding_model: ...                          # one model for index + query
  embedding_dimensions: ...
  rerank_model: ...                             # used when rerank is enabled
  hybrid: {bm25_weight: ..., vector_weight: ...}
  graph_rag: {enabled: false}
sources: [ ... connector configs ... ]
acl: {matter_visibility: union | matter_restricted, decision_visibility: firmwide}
```

Every model call resolves through the gateway (LiteLLM-style) and each pipeline
stage carries its own model assignment, so "which model for insertion" is a dropdown,
air-gapped deployments point at vLLM/Ollama endpoints, and nothing in pipeline code
knows about providers.

## 5. Component selection

Components: converter stack, sync building blocks, queue/orchestration,
vector/hybrid index, embedding server + models, structured extraction, PII/anonymization,
graph layer (if any), MCP framework, UI stack, eval harness. Criteria: permissive
license (MIT/Apache/BSD; GPL/AGPL only as unmodified sidecars), alive, boring to
operate on one VM, German-capable.
