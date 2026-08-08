---
title: Insertion pipeline
description: "Technical reference for the staged, durable, per-document insertion pipeline: stages, processing states, retry and quarantine, orchestration, configuration, and the console controls."
---

The insertion pipeline turns a synced file into structured, searchable knowledge. It is
a per-document state machine: every source object carries one `processing_state` row per
stage, and those rows, not the orchestrator, are the source of truth for what has run,
what failed, and what is still owed. A worker crash, an orchestrator migration, or a
re-triggered run resumes from the durable rows and never repeats completed work.

The console page for this feature is **Insertion pipeline**; the gateway model each
stage calls is assigned under [Models & services](/product/models-and-services/).

## Stages in execution order

Seven stages run per document, in this order (`PIPELINE_STAGE_ORDER` in
`taxonomies.py`). The console shows the same seven cards. An eighth stage id,
`gen_evals`, still exists in configuration for backwards compatibility but is no longer
part of the insertion DAG; its handler drains any leftover rows as skipped
(`gen_evals_moved_to_environment_builder`).

| # | Stage id | Console name | Executed by | Model |
| --- | --- | --- | --- | --- |
| 1 | `fetch` | Fetch | Source connector | n/a |
| 2 | `convert` | Parse | Docling Serve (plus built-in text/email/OOXML converters) | n/a |
| 3 | `classify_matter` | Classify | Model gateway (agent loop) | `pipeline.stages.classify_matter.model` |
| 4 | `relate` | Relate | Model gateway (agent loop) | `pipeline.stages.relate.model` |
| 5 | `extract_metadata` | Extract metadata | Model gateway (agent loop) | `pipeline.stages.extract_metadata.model` |
| 6 | `extract_decisions` | Extract decisions | Model gateway (single structured call) | `pipeline.stages.extract_decisions.model` |
| 7 | `index` | Index | Embedding model + OpenSearch | `retrieval.embedding_model` |

### Fetch

Reads the object's bytes through its source connector (a connector that staged content
during the scan is read locally via `open_staged`; otherwise `fetch(external_id)`).
Writes the bytes into the artifact store, enforcing `pipeline.max_file_mb`, and records
a `Blob` row keyed by content hash. Sets `source_object.content_hash`. A deleted source
object is skipped with reason `source_object_deleted`. Idempotency: blobs are
content-addressed; re-running re-reads the bytes but converges on the same hash.

### Parse (`convert`)

Reads the fetched blob from the artifact store. Plain-text formats are decoded directly;
`.eml` / RFC 822 mail is parsed with the standard library (headers, participants,
Message-ID chain, attachment names); everything else goes to Docling Serve
(`components.docling_url`) with OCR pinned to `easyocr` for German and English,
accurate table mode, and a 270-second per-document timeout. `.docx` files additionally
get a raw-OOXML pass that extracts tracked changes (`w:ins`/`w:del`/moves, with author
and date) and comments, because the converted text is the accepted view and deleted text
exists nowhere else. Output is one `Artifact(kind="structured_json")` keyed by content
hash, containing `text`, `metadata`, and `revisions`.

Failure contract: a Docling 4xx, a `partial_success` with errors, or an invalid DOCX
raises `UnsupportedDocument` and quarantines immediately; a document is never indexed
half-converted. 5xx and network errors stay retryable. Idempotency: if a
`structured_json` artifact already exists for the content hash, the stage returns done
without calling Docling.

### Classify (`classify_matter`)

Assigns the file to a matter. See [Agentic stages](#agentic-stages-classify-and-relate)
for the exact context. Reads the `structured_json` artifact; writes or updates the
`MatterAssignment` for the source object, creates the `Matter` when none fits (with an
advisory lock per `(project, reference)` so parallel workers converge on one matter),
stores an `Artifact(kind="classification")` per content hash, and replays any relation
intents other files had parked against this one. When the Area-of-Law or Service facets
are active it also sets the matter's practice area and kind (first valid value wins;
matters imported from practice management stay authoritative).

### Relate (`relate`)

Establishes document identity (new document / new version / duplicate), version order,
redline links, typed relations (`annex_of`, `responds_to`, `references`, `amends`), and
email threads. Requires a matter assignment (retryable until Classify has run). The
model call runs with no database transaction or lock open; afterwards the stage takes
sorted per-file advisory locks on every file it will write, re-validates that the claim,
content hash, and matter assignment are unchanged (any drift raises a retryable error),
and materializes the result: `Document` and `DocumentVersion` rows, `supersedes` edges,
`redline_against` pointers, `Relation` rows, and `CommunicationThread` membership.
Decisions whose target file has no matter assignment yet are parked as `RelationIntent`
rows and replayed when that file classifies.

### Extract metadata (`extract_metadata`)

Runs on every version, drafts included; this stage is the sole owner of document
typing. An agent walks the active document-type ontology with navigation tools
(`ontology_roots` / `ontology_children` / `ontology_node` / `ontology_search`) and may
only submit a type node id that appeared in a tool result and is inside the active
scope; when the clause facet is active, notable clauses are typed the same way through
`clause_search`. Writes `doc_type`, `doc_type_ancestors`, the ontology fingerprint,
language, `doc_date` (from content, falling back to file mtime), title, parties, and
identifiers onto the document; stores an `Artifact(kind="notable_clauses")` per content
hash and one `Extraction` provenance row per document. An untyped result is recorded
honestly (`doc_type = NULL`) with the fingerprint of the scope that judged it, so a
later, richer ontology re-types exactly those documents.

### Extract decisions (`extract_decisions`)

Reads the `structured_json` artifact's `revisions` and `comments`. With no revision
evidence the stage skips (`no_revision_evidence`); with evidence but no extractable
decision it skips (`no_decision_evidence`). Otherwise one structured call (`chat_json`)
produces a `DecisionRecord` with locus, change summary, rationale category (validated
against the taxonomy), rationale text and generalizability, linked to the version pair
(`redline_against` → current version). Idempotency: an existing record for the same
document and target version makes the stage a no-op.

### Index (`index`)

Chunks the converted text (`retrieval.chunk_chars` / `chunk_overlap_chars`), embeds
each chunk with `retrieval.embedding_model` (with an optional context header naming title, type,
and matter), and writes `Chunk` rows plus an OpenSearch bulk sync. Additional rows: a
profile embedding at ordinal −1 for the latest final version and clause embeddings at
ordinals 1000+ for final/executed versions. Every chunk carries a denormalized
permission projection (`allowed_principals` / `denied_principals`) compiled from source,
project, and document grants. Re-indexing diffs against existing chunks by ordinal:
replaced, not orphaned. When a sync detected an ACL-only change, the row carries the
durable marker `access_only_reindex` and the handler updates only the permission fields
and bumps `access_version`, without re-splitting or re-embedding anything.

## Processing states and console buckets

Stored statuses (`ProcessingStatus`): `pending`, `running`, `done`, `failed`,
`quarantined`, `skipped`.

Three different situations are *stored* as `skipped`, distinguished by the
`last_error.reason` field, and the reporting layer (`stage_bucket()` in
`taxonomies.py`; the same CASE expression in `GET /api/status`) splits them into
separate buckets so none of them reads as a handler decision it was not:

| Bucket (API) | Console legend | Stored as | Meaning |
| --- | --- | --- | --- |
| `pending` | queued | `pending` | Eligible for a worker to claim. |
| `running` | running | `running` | Claimed by a worker (claim expires after `claim_timeout_seconds`). |
| `done` | done | `done` | Handler completed; `producer_version` recorded. |
| `failed` | awaiting retry | `failed` | Failed non-deterministically with attempts left; retried when `next_retry_at` is due. |
| `waiting` | waiting on the stage before | `skipped` + reason `waiting_for_previous_stage` | Parked behind an unfinished predecessor. Stored as `skipped` so the claim query cannot hand it to a worker; displayed as waiting because that is what it is. |
| `disabled` | skipped, stage is off | `skipped` + reason `disabled_by_configuration` | The stage was switched off in config when the claim executed. |
| `skipped` | skipped | `skipped` + a handler reason | The handler's own judgement: nothing to do (e.g. `no_revision_evidence`, `source_object_deleted`). |
| `quarantined` | quarantined | `quarantined` | Terminal. Only the explicit retry endpoint releases it. |

Lifecycle: a scan seeds one row per stage per object, `fetch` as `pending`, every
later stage as `skipped`/`waiting_for_previous_stage`. When a stage finishes,
`_unlock_next` flips the next stage's waiting row to `pending`. The claim query takes
the oldest eligible row (`pending`, or `failed` with its retry due), ordered by
`updated_at`, with `FOR UPDATE SKIP LOCKED` on Postgres so concurrent workers never
double-claim.

## Retry policy and quarantine

- **Claiming** increments `attempts`. On failure the row records the error and either
  retries or quarantines.
- **Backoff**: `next_retry_at = now + retry_base_seconds × 2^(attempts−1)`. With the
  defaults (base 5 s, `max_attempts` 3) that is 5 s, then 10 s, then quarantine.
- **Attempt limit**: `pipeline.stages.<stage>.max_attempts` (default 3, range 1–20),
  per stage, editable per stage card in the console ("Attempts before quarantine").

Error classification in `_execute_claim`:

| Error | Treatment |
| --- | --- |
| `UnsupportedDocument`, `ArtifactTooLarge`, `FileNotFoundError`, `ValueError` (includes `ProviderPermanentError`) | **Deterministic**: quarantined on the first attempt; retrying cannot change the outcome. |
| `ModelOutputInvalid` (schema-invalid or non-converging model output) | Retried with backoff up to `max_attempts`, since truncated or malformed model responses are commonly transient. |
| Any other exception (network faults, 5xx, `RetryableStageError`, `StaleClaim`) | Retried with backoff up to `max_attempts`, then quarantined. |

`ProviderPermanentError` deserves a note: a rejected API key (401), a forbidden model
(403), a model the gateway does not serve (404), or an exhausted account quota
(`insufficient_quota` inside a 429) is raised as a deterministic failure, so a document
quarantines immediately with a readable cause instead of spending its backoff budget on
a fault that will still be there. A process-wide cooldown
(`KI_PROVIDER_FAULT_COOLDOWN_SECONDS`, default 60) makes subsequent documents against
the same dead model fail fast without touching the network.

**What quarantine records** (`processing_state.last_error`): the exception class, the
message (first 2 000 characters), the traceback tail (last 6 000 characters), and a
`deterministic` flag. The row also keeps `attempts` and `updated_at`. `GET
/api/quarantine` returns path, stage, attempts, and this error object.

**Retry from quarantine** (`POST /api/quarantine/{source_object_id}/retry?stage=`):
quarantine is otherwise terminal; no producer-version bump reclaims a quarantined row.
The retry releases the *earliest* quarantined stage (a later requested stage would
re-run downstream work on an input that is still missing), resets its attempts to zero
(a fresh budget, not a bypass), invalidates every downstream stage back to waiting, and
immediately launches an insertion run so the requeued row does not sit pending forever.
The response reports the released stage, the invalidated stages, `max_attempts`, the
previous error, and whether the original failure was deterministic. A deterministic
failure will quarantine again on the first attempt unless the file itself changed, and
the console's retry button says so in its tooltip. Each retry is written to the audit
log with the overruled error.

**Stale claims**: a row left `running` longer than `claim_timeout_seconds` (default
900 s), meaning a worker died mid-stage, is flipped to `failed` with error class `StaleClaim`
and retried immediately. This runs at the start of every run
(`recover_stale_claims`).

## Orchestration

`components.orchestrator_provider` selects the executor: `hatchet` (default) or
`local`. Any other value fails closed; nothing silently runs the corpus in-process.
All triggers (the console, the sync handoff, the folder watcher, the quarantine retry)
go through one implementation, `orchestration/insertion.py::launch_insertion`, which
creates a `pipeline_runs` ledger row and hands it to the provider.

**In-process provider (`local`)**: `PipelineRunner.run_until_idle()` inside the API
process: prepare steps (`requeue_outdated_stages`, `requeue_newly_enabled_stages`,
`recover_stale_claims`), then claim-execute in a loop until no eligible row remains.
The run row records the scalar counters (`processed`, `done`, `skipped`, `retried`,
`quarantined`).

**Hatchet provider**: `trigger_insertion` runs the same prepare steps, then atomically
reserves a batch of every non-deleted object with a non-terminal stage row that is not
already owned by another active batch (under a global advisory lock, so concurrent
handoffs never start a second DAG for the same documents), and bulk-triggers one
workflow run *per document* with one visible task per stage. Each task calls
`run_stage_for_object(stage, id)`, which claims at most one row; the database row
remains the idempotency boundary, so a Hatchet replay or workflow migration sees the
durable state and no-ops over completed stages. Tasks have their own transport-level
retry (6 retries, backoff factor 5, capped at 60 s, six-hour schedule and execution
timeouts) layered on top of the row-level retry policy. A batch consisting solely of
`access_only_reindex` index rows runs the lighter access-refresh workflow instead and
is labelled "Access refresh" in the run ledger. Batch progress is aggregated into the
run row under a per-run advisory lock; the final task forces the last refresh so a run
reaches `completed` rather than 99.9 %.

Concurrency and sweeper environment variables:

| Variable | Default | Effect |
| --- | --- | --- |
| `KI_HATCHET_DOCUMENT_CONCURRENCY` | 16 (max 1024) | How many document DAGs advance concurrently (GROUP_ROUND_ROBIN across the batch). |
| `KI_RELATE_MODEL_CONCURRENCY` | 16 (max 256) | Additional cap on concurrent `relate` tasks, the long agentic stage. |
| `KI_HATCHET_SYNC_CONCURRENCY` | 4 (max 64) | Concurrent source scans. |
| `KI_RUN_SWEEP_SECONDS` | 300 (`0` disables) | Interval of the worker-side run sweeper. |
| `KI_RUN_SILENT_MINUTES` | 15 | Silence before the sweeper examines a run at all. |
| `KI_RUN_ABANDONED_HOURS` | 7 | Silence before an unverifiable run is failed. |

**The sweeper** (`orchestration/sweeper.py`) resolves `pipeline_runs` rows that nothing
will ever advance: a worker replaced mid-stage, a task that exhausted retries between
progress writes. It runs on a timer inside the Hatchet worker and opportunistically
from `GET /api/status` and `GET /api/runs`. Liveness is read from what the pipeline
already writes (`pipeline_runs.updated_at` plus, for insertion batches, the newest
`processing_state.updated_at` in the batch); a run silent for less than
`KI_RUN_SILENT_MINUTES` is not even examined. A batch whose objects are all terminal is
completed by re-running the pipeline's own aggregation. Otherwise the orchestrator is
asked: "no such workflow run" fails the run now; a terminal verdict fails a sync run
(for an insertion batch only absence is conclusive, since the recorded id is just the
first workflow of a bulk trigger); when the orchestrator cannot be asked, the run is
failed only after `KI_RUN_ABANDONED_HOURS` of silence (deliberately longer than the
longest allowed task) and always with the recorded cause, never deleted. This matters
because `uq_pipeline_runs_active_sync` refuses a second sync of a source with an
unfinished sync run: a stranded run would otherwise stop indexing silently.

## Configuration

All fields live under `pipeline.*` in the saved configuration; scalar fields can be
pinned by environment variables using the `KI_` prefix and `__` as the nesting
delimiter (an environment-pinned setting refuses console edits with a 409 rather than
silently losing them).

| Field | Env var | Default | Effect |
| --- | --- | --- | --- |
| `pipeline.max_file_mb` | `KI_PIPELINE__MAX_FILE_MB` | 512 | Fetch limit; a larger blob raises `ArtifactTooLarge` and quarantines deterministically. |
| `pipeline.claim_timeout_seconds` | `KI_PIPELINE__CLAIM_TIMEOUT_SECONDS` | 900 | A `running` claim older than this is failed as `StaleClaim` and retried. |
| `pipeline.retry_base_seconds` | `KI_PIPELINE__RETRY_BASE_SECONDS` | 5 | Base of the exponential backoff (`base × 2^(attempts−1)`). |
| `pipeline.inline_conversion_budget_seconds` | `KI_PIPELINE__INLINE_CONVERSION_BUDGET_SECONDS` | 90 | Time budget for one relate `open_file` call pulling a neighbour's fetch/convert forward. |
| `pipeline.inline_conversion_slots` | `KI_PIPELINE__INLINE_CONVERSION_SLOTS` | 4 | Process-wide cap on concurrent inline conversions, so the stage-level Docling concurrency plan keeps meaning. |
| `pipeline.auto_insert_after_sync` | `KI_PIPELINE__AUTO_INSERT_AFTER_SYNC` | `true` | A sync that brought something new hands off to an insertion run automatically. Off: the sync completes with a null `insertion_run_id` and an operator starts insertion explicitly (e.g. to review a scanned estate before paying for conversion and embedding). |
| `pipeline.deletion_confirmations` | `KI_PIPELINE__DELETION_CONFIRMATIONS` | 3 (1–20) | A mass deletion is applied only after this many consecutive scans report the identical missing set; documents stay searchable meanwhile. `1` tombstones on the first scan. |
| `pipeline.stages.<stage>.enabled` | n/a (per-stage map) | `true` (`gen_evals`: `false`) | A disabled stage parks its rows as `disabled_by_configuration`; later stages run without its output. |
| `pipeline.stages.<stage>.model` | n/a | `$KI_LLM_MODEL` | The gateway model the stage calls; read by the model-calling stages, assigned on [Models & services](/product/models-and-services/). |
| `pipeline.stages.<stage>.max_attempts` | n/a | 3 (1–20) | Attempts before quarantine for non-deterministic failures. |
| `pipeline.stages.<stage>.rerun_token` | n/a | `""` | Operator-owned re-run marker; set by the console's "Re-run all files" dialog. |
| `pipeline.stages.<stage>.producer_version` | n/a | computed | Effective stage version, never a setting; see below. |

### `producer_version` semantics

Every completed stage row records the `producer_version` it ran under. The effective
version is computed on every config load as `<code version>[+<rerun_token>]`: the code
version is owned by the implementation (`CODE_STAGE_VERSIONS`, currently
`classify_matter` mvp-3, `relate` mvp-10, `extract_metadata` mvp-2, everything else
mvp-1), and whatever a saved config file holds for `producer_version` is discarded
rather than obeyed. This means a release that changes a stage's behaviour reaches
documents already on disk, and an operator's re-run (which bumps `rerun_token`)
survives a release; the two levers cannot overwrite each other.

At the start of every run, `requeue_outdated_stages` finds each object's earliest
`done`/`skipped` stage whose recorded version differs from the effective one, requeues
it with a fresh attempt budget, and parks every downstream stage back to waiting. A
stage's output is the next stage's input, so re-running one without invalidating what
was derived from it would leave the document half-old. Rows still waiting (no recorded
version) are untouched; quarantined rows are never reclaimed this way.
`requeue_newly_enabled_stages` similarly turns `disabled_by_configuration` rows back to
pending once the stage is re-enabled. Ontology scope changes use a narrower path
(`requeue_ontology_outdated`): only documents whose type node fell out of the visible
scope, or that were left untyped under a different scope fingerprint, get
`extract_metadata` and downstream requeued.

## The console page

Everything on the page is driven by these endpoints:

| Control / element | Endpoint | Notes |
| --- | --- | --- |
| Stage bars and buckets, run banner | `GET /api/status` (polled every 5 s while a run is active) | Returns the bucketed per-stage counts and active runs; also triggers an opportunistic sweep. |
| Stage settings, run settings | `GET /api/config` → edits → `PUT /api/config` | All edits accumulate in an unsaved-changes bar before anything is written. |
| Save / Save and re-run | `PUT /api/config`, then `POST /api/actions/pipeline` only when a change requires a run | See below. |
| "Re-run all files" (per stage) | `PUT /api/config` (bumps that stage's `rerun_token`) + `POST /api/actions/pipeline` | The dialog states how many files re-run, which downstream stages re-run too, and the stage's realized model spend so far. |
| Stage model chip | `GET /api/models/catalog` | Resolves the stage's assigned gateway name to the model that will actually run and be billed; falls back to the bare alias when the gateway cannot resolve it. |
| Per-stage cost in the re-run dialog | `GET /api/costs` | Realized spend per stage from the usage ledger. |
| Recent processing runs | `GET /api/runs` | Filtered to insertion and access-refresh workflows. |
| Quarantined files table | `GET /api/quarantine` | Path, stage, error class/message, attempts. |
| Retry (per quarantined file) | `POST /api/quarantine/{id}/retry?stage=` | Releases the stage, invalidates downstream, starts a run. |
| "Start after every sync…" checkbox | `pipeline.auto_insert_after_sync` via `PUT /api/config` | |

**What "save" re-runs and what it does not.** Saving alone only writes configuration.
The button becomes "Save and re-run", and a `POST /api/actions/pipeline` follows the
save, exactly when a pending change requires a run to take effect: re-enabling a stage
(the parked files are queued again) or a stage version bump. Disabling a stage,
changing `max_attempts`, the auto-insert toggle, max file size, claim timeout, and
retry backoff save without starting a run. This is deliberate: a bumped version or a
re-enabled stage only marks rows pending, and nothing picks them up until a run begins;
saving such a change without a run would strand the work with nothing on screen
saying so.

The "Re-run all files" count covers rows in the `done`, `skipped`, and `disabled`
buckets, the rows that recorded a version. `waiting` rows have not been reached and are
not part of a re-run.

## Agentic stages: Classify and Relate

Both stages run a bounded tool-calling loop (`chat_agent`): the model gathers evidence
with tools and finishes by calling a synthetic `submit_result` tool whose arguments are
the stage's Pydantic schema. A schema-invalid or validator-rejected submission is fed
back into the loop for correction; a loop that never converges raises
`ModelOutputInvalid` and the stage retries as a whole. Every attempt gets a fresh trace
id, tagged onto each gateway call (`doc:`, `stage:`, `trace:`) and written into the
result's provenance, so each database row links to its exact gateway trace.

### Classify: what the model is given

Prompt payload: the file's path, filename, parent folder, a rendered folder
neighbourhood (the file's own folder in full plus two levels up and down; sibling
folders of the ancestor spine appear name-only with size hints), up to 200 known
document titles, and the first 8 000 characters of converted text.

Tools:

- `search_matters(query, limit=8, offset=0)`: ranks existing matters by semantic
  similarity over already-indexed chunks, title substring, and reference-number
  match. Returns `{results, page}`; the whole candidate set is ranked before the
  page is cut, so `page.total` is exact and an `offset` walks the same ranking.
  A full page with `has_more` is explicitly *not* grounds to create a new matter —
  that mistake splits one file in two, so the tool description says so.
- `peek_matter(matter_id)`: one matter's references, practice area, folders, and a
  sample of document titles — with `document_count` (the true total) and
  `document_titles_are_sample`, so a 12-title list is not read as the whole matter.
- `list_folder()`: the ±2-level neighbourhood again.
- `create_matter(reference_number, title)`: get-or-create, committed in its own
  session immediately so concurrently classifying documents of the same matter see it
  the moment the tool returns.
- `service_*` tools (when the Service facet is active): search/browse/verify service
  nodes by definition for the matter kind.

The result validator enforces that a submitted `matter_id` appeared in a tool result
(never invented), that a practice-area node is on the offered menu, and that a
matter-kind node was actually visited and is in the active facet. A null or dangling
matter id falls back to a reference-number scan under an advisory lock, and failing
that a new matter with a stable placeholder reference derived from the top-level folder
(`UNASSIGNED-<slug>`).

### Relate: what the model is given

Prompt payload: the assigned matter's reference numbers and title, the current file's
path and full converted text, a tracked-changes digest (up to 40 revisions / 4 000
characters, longest first, with the total count so the model knows what the sample
omits), email headers when the file is an email, and a complete folder listing one
level above and below the file's folder.

Tools (each on its own short database session, because a relate call can spend minutes on
model I/O and must not hold a transaction):

- `list_folder(path)`: look inside any folder, including the name-only siblings;
  `/` lists the source root.
- `search_documents(query, limit=10, offset=0)`: corpus-wide document search
  (semantic + title) for targets outside the neighbourhood. Returns
  `{results, page}` with an exact `page.total`.
- `open_file(path, offset, max_chars)`: pageable converted text of any listed file,
  with its own tracked-changes digest. The result carries `has_more` and
  `next_offset` rather than leaving the model to compare `offset + returned_chars`
  against `total_chars` — a long file read once used to look like a file read
  whole. If the target has not converted yet, the stage pulls its
  `fetch`/`convert` forward through the normal claim machinery
  (`ensure_source_object_ready`), bounded by `inline_conversion_budget_seconds` and
  `inline_conversion_slots`, observing rather than duplicating a claim another worker
  holds, and returning an honest status (`busy`, `in_progress`, `quarantined`, …) the
  model can act on when the file cannot be made ready in time; the pair is then linked
  from the other file's side when that file relates.

The result validator rejects any target ref the model did not actually open; refs come
only from `open_file` results, never from paths. Materialization holds sorted advisory
locks on all touched files (so two files that reference each other cannot deadlock),
and an entity abandoned by a duplicate/new-version decision is folded into its survivor
rather than left behind as an empty document.

Extract metadata uses the same loop-and-validator mechanics against the ontology (only
visited, in-scope node ids are accepted), but its context is the single document, with no
neighbourhood or corpus search.

## Failure modes

- **Poison files** (corrupt PDF, invalid DOCX, a format Docling rejects): quarantined
  deterministically at Parse with the converter's reason. Because every document is its
  own workflow and quarantine is per-row, one poisoned file never blocks the rest of
  the corpus.
- **Oversized files**: `ArtifactTooLarge` at Fetch when the stream exceeds
  `max_file_mb`; deterministic quarantine.
- **Truncated conversions**: Docling `partial_success` with errors is treated as
  failure, not as degraded success; half a document is never silently indexed.
- **Schema-invalid model output**: corrected inside the agent loop where possible;
  otherwise `ModelOutputInvalid`, retried with backoff up to `max_attempts` because
  such responses are commonly transient, then quarantined.
- **Dead credentials or exhausted quota**: `ProviderPermanentError`, deterministic
  quarantine on the first attempt with a cause written for the person who has to fix
  it; a short cooldown fails sibling documents fast. After the account is fixed, the
  quarantine retry releases the files.
- **Worker death mid-stage**: the claim expires after `claim_timeout_seconds` and the
  row is retried (`StaleClaim`). Under Hatchet, the task layer retries independently;
  the replay claims the durable row and resumes at the first unfinished stage;
  completed artifacts (conversions, classifications, chunks) are keyed by content hash
  or diffed in place and are never redone.
- **Run rows nothing will advance**: resolved by the sweeper as described above,
  completed when the work actually finished, failed with the recorded cause otherwise,
  never deleted.
- **A file whose content changes mid-relate**: the post-inference re-validation detects
  the changed content hash or matter assignment and retries the stage against the new
  state.

## Related

- [Models & services](/product/models-and-services/): which model each stage is assigned,
  embedding index management.
- [Costs](/product/costs/): the usage ledger behind the per-stage spend shown in the
  re-run dialog.
- [Ontology](/product/ontology/): the artifact and scope the typing walk runs
  against, and what a scope change re-types.
- [Architecture](/concepts/architecture/): the durability contract in context.
