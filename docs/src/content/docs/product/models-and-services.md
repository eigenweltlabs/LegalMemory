---
title: Models & services
description: Gateway models and the stages and features they are assigned to, gateway model registration, the embedding lock and index rebuild, retrieval tuning, ingestion signals, and the service registry.
---

**Models & services** is the console page for everything LegalMemory computes
with: which model does which job, the embedding index that binds one of those
models, the retrieval and ingestion knobs, and the registry of backing
services. Everything on the page reads and writes the same configuration
served by `GET /api/config` and saved with `PUT /api/config`.

## Model assignments

LegalMemory never names a model in code, and there is no intermediate layer of
named model roles. Models live in the LiteLLM gateway; each pipeline stage that
calls a model carries its own assignment (`pipeline.stages.<stage>.model`), and
the features outside the pipeline carry theirs in their own configuration.
Every assignment defaults to `$KI_LLM_MODEL`, except the embedding model,
which defaults to `$KI_EMBEDDING_MODEL`. An unset assignment is empty; the
console shows "Select a model…" and every caller fails loudly rather than
guessing at a model and billing for it.

| Assignment | Used by (verified call sites) |
| --- | --- |
| `pipeline.stages.classify_matter.model` | The **Classify** stage's matter-classification agent (`PipelineRunner._classify_matter`). |
| `pipeline.stages.relate.model` | The **Relate** stage's file-relation agent, typically the largest spend. |
| `pipeline.stages.extract_metadata.model` | The **Extract metadata** stage, and [firm-billing extraction](/product/costs/) (`POST /api/actions/extract-billing`), which is metadata extraction over billing documents. |
| `pipeline.stages.extract_decisions.model` | The **Extract decisions** stage. |
| `pipeline.stages.gen_evals.model` | The RL-environment builder (the standalone `gen_evals` flow). |
| `retrieval.embedding_model` | The **Index** stage (chunk, profile and clause vectors), the query embedding of the semantic search leg, and the corpus-wide matter search that runs during classification. One model for the whole appliance; see the embedding lock below. |
| `retrieval.rerank_model` | Search-time re-ranking, only when `retrieval.rerank_enabled` is on. |
| `ask_model` | `POST /api/ask`: retrieval planning and answer synthesis. |

An assignment is nothing but the model's gateway-served name. Every call goes
to `components.litellm_url` with the gateway master key
(`LITELLM_MASTER_KEY`), at temperature `0.0`; pipeline output is
deterministic by design. The gateway's `drop_params: true` removes parameters
a concrete model rejects.

### How an assignment resolves through the gateway

An assignment stores an **alias**, not a provider model id. The gateway's
`model_list` (`deploy/litellm/config.yaml`) maps each alias to an upstream
route (`os.environ/KI_LLM_UPSTREAM` and friends), a key, and declared
per-token rates. Several aliases may point at one upstream model.

The console un-aliases through `GET /api/models/catalog`: the app proxies the
gateway's `/model/info` using the master key (the key never reaches the
browser) and returns, per model, only an allow-listed projection: `id` (the
alias), `upstream_model`, `api_base`, `mode` (`chat` / `embedding` /
`rerank`), and `source` (`config` for models declared in the gateway config
file, `runtime` for models added from the console). `litellm_params` is
dropped wholesale, because that is the structure that carries provider keys.

Assignment dropdowns are fed from this catalog and filtered by mode: the
embedding picker only offers embedding models, every other assignment only
chat models. Options
are labelled `upstream_model · via alias`, so what actually runs leads and
the alias trails it. A saved value the gateway does not currently serve stays
in the list, labelled either "not served by this gateway" or "gateway
unreachable"; a temporary outage never rewrites the saved configuration.

## Adding a model

**Add model** registers a new alias with the gateway at runtime
(`POST /api/models/catalog` → gateway `POST /model/new`). The form takes an
alias, an upstream model id (e.g. `openai/gpt-4o-mini`), a **named
credential**, an optional API base, and the mode (which decides where it can
be assigned).

Key handling is the point of the design:

- The gateway config declares **named credentials** (`credential_list` in
  `deploy/litellm/config.yaml`). Each entry references its key with
  `os.environ/…`, which the gateway resolves only for entries in that file,
  so the actual secrets live exclusively in the gateway container's
  environment.
- A runtime model references a credential **by name**
  (`litellm_credential_name`). The registration schema deliberately has no
  API-key field: raw provider keys are never accepted by a browser form, a
  request body, or LegalMemory's database.
- The app validates the credential name against the gateway's `/credentials`
  endpoint before registering (`400` for an unknown name, `502` if the
  gateway is unreachable), because the gateway itself would accept an unknown
  name and only fail at call time, which would quarantine documents for the
  wrong reason.
- `GET /api/models/catalog` returns credential **names and descriptions**
  only; values never cross that boundary.

**Where things are stored.** The alias and its routing parameters are stored
in the gateway's own database; this requires `store_model_in_db: true` in
the gateway config, matched by `STORE_MODEL_IN_DB: "True"` in
`docker-compose.yml`. When it is off, LiteLLM refuses the registration and
that message is passed through verbatim. LegalMemory's database stores only
an audit event (`models.register`) recording the alias, upstream model,
mode, and credential *name*: auditable provider wiring, no secret.
Config-file models cannot be edited or removed from the console; runtime
models can.

## The embedding lock and the rebuild job

Vectors from two embedding models must never share one ANN index, so
`retrieval.embedding_model` is locked by a single condition, reported by
`GET /api/index/status`:

```
locked = chunk_count > 0
```

i.e. as soon as at least one chunk row exists, the console disables the
embedding-model select and the dimension field until a rebuild.

### Model-bound index naming

`retrieval.index_name` starts as `knowledge-index-chunks-v1`. The rebuild
target is derived from the **embedding signature**, the identity of the
vectors an index may hold:

```
embedding_signature = slug(retrieval.embedding_model) + "-" + retrieval.embedding_dimensions
derived_index_name  = "knowledge-index-chunks-" + embedding_signature
```

`slug()` lowercases the model name and replaces every non-alphanumeric
character with `-` (collapsing runs), so e.g. `text-embedding-3-small` at
1536 dimensions targets `knowledge-index-chunks-text-embedding-3-small-1536`.
Switching model or dimension therefore always targets a fresh, uniform index.

### What the rebuild does

**Rebuild vector index** on this page saves the draft configuration and calls
`POST /api/actions/reindex`, which:

1. Sets `retrieval.index_name` to the derived, model-bound name.
2. Bumps the Index stage's operator-owned `rerun_token`, deliberately not
   `producer_version`, which is recomputed from the code's own version on
   every config load and would discard the bump, requeuing nothing.
3. Requeues the `index` stage for every object (`requeue_outdated_stages`).
4. Launches an insertion run and returns the target index, model, dimensions,
   the number of chunks to re-embed, and the run id.

Guardrails, all in code:

- **Only the Index stage replays.** Re-embedding reads the stored
  `structured_json` artifacts; conversion, classification, relation and
  extraction results are untouched.
- **Existing chunk rows are diffed, not orphaned**: body chunks (ordinals
  0..n), the profile row (−1) and clause rows (1000+) all participate in the
  diff, so re-indexing replaces rows and deletes obsolete ones.
- **Dimension guard**: on first use of an existing index, the adapter reads
  the live mapping and raises if the index was built for a different
  embedding dimension than the configured model produces, with instructions
  to rebuild. Nothing degrades silently.
- Same-dimension model changes cannot mix either, because the index *name*
  is bound to the model slug.

**Queries during a rebuild** run against the new model-bound index from the
moment the config is saved; the name switch is immediate, and the index is
created on first touch. Documents become searchable again as their Index
stage completes; the old index is left in place but is no longer queried.

### Embedding & vector index settings

| Setting | Config key | Default | Effect |
| --- | --- | --- | --- |
| Embedding dimensions | `retrieval.embedding_dimensions` | `1536` | Must match the model's output; enforced per call (`embed_text` raises on mismatch) and per index (mapping guard). Locked together with the model. |
| Vector engine | `retrieval.vector_engine` | `lucene` | HNSW engine for the kNN field. Lucene does native pre-filtered kNN (every leg is ACL-filtered) and keeps the graph in the Lucene segment; `faiss` is for multi-million-vector scale or quantization. |
| Space type | `retrieval.vector_space_type` | `cosinesimil` | Distance metric of the kNN field (faiss has no native cosine). |
| HNSW m | `retrieval.hnsw_m` | `16` | Graph fan-out at index build. |
| HNSW ef_construction | `retrieval.hnsw_ef_construction` | `128` | Build-time beam width; higher improves recall at indexing cost. |

Vector search is always approximate (HNSW), never brute-force
`script_score`.

## Fusion & ranking

A query runs three ACL-scoped ranked legs in a single `_msearch` round-trip:
lexical (BM25 over chunk text), semantic (kNN over the embedding), and
identifier (query text matched against model-extracted identifiers). It
fuses them with reciprocal-rank fusion, per chunk id:

```
score(chunk) += weight_leg / (fusion_rrf_k + rank_in_leg)
```

then multiplies by the version-status boost, collapses per document,
re-verifies authorization in SQL, and optionally reranks.

| Setting | Default | Effect |
| --- | --- | --- |
| `retrieval.fusion_rrf_k` | `60` | RRF constant `k` (1–1000). Lower sharpens the contrast between top ranks. |
| `retrieval.weight_lexical` | `1.0` | Weight of the BM25 leg in fusion. |
| `retrieval.weight_semantic` | `1.0` | Weight of the vector leg. |
| `retrieval.weight_identifier` | `1.5` | Weight of the identifier leg; a pasted case or file number matches its document without any regex parsing of the query. |
| `retrieval.weight_decisions` | `0.8` | Declared and editable, but not applied by the current three-leg fusion; drafting-decision search is a separate tool (`search_decisions`), not a fused leg. |
| `retrieval.version_status_boost` | `executed 1.2 · final 1.0 · unknown 0.8 · draft 0.7` | Multiplier on the fused score by version status: legal authority decays by supersession, not by age. A status not in the map multiplies by 1.0. |
| `retrieval.collapse_per_document` | `true` | Keep the single strongest chunk per document (ties prefer the latest final version) instead of a chunk flood. Off, up to `max_chunks_per_document` chunks per document survive. |
| `retrieval.max_chunks_per_document` | `3` | Cap per document when collapse is off (1–20). |
| `retrieval.rerank_enabled` | `false` | Sends the top-20 fused candidates to `retrieval.rerank_model` for 0–10 relevance scoring and reorders by it. A gateway error raises; there is no silent fallback to the fused order. |
| `retrieval.graph_rag_enabled` | `false` | Declared; no code path currently reads it. |

Fusion never sees an unauthorized row: every leg runs inside the compiled
access scope, and the SQL re-verification is the authoritative backstop.

## Ingestion signals

What the Index stage writes per document version, and how the settings change
it:

| Setting | Default | Effect in the indexer |
| --- | --- | --- |
| `retrieval.chunk_chars` | `1200` | Body chunk size in characters (200–10000). |
| `retrieval.chunk_overlap_chars` | `120` | Overlap between adjacent body chunks (0–2000). |
| `retrieval.chunk_contextualize` | `true` | Prefixes each chunk with a context header (title, human-readable document-type label, matter title) **before embedding only**; the stored and displayed chunk text stays raw. |
| `retrieval.profile_embeddings` | `true` | Adds one document-profile row (ordinal −1) per **latest final** version: title, type, matter, reference numbers, parties, identifiers, date and leading text, embedded as a single document-level vector. |
| `retrieval.clause_embeddings` | `true` | Adds one row per notable clause (ordinals 1000+) for **final/executed** versions, from the `notable_clauses` artifact produced by metadata extraction, carrying `clause_type` and locus. |

All three row kinds participate in the existing-chunk diff, so re-indexing
replaces rather than orphans them. Every chunk also carries its compiled
allow/deny principals, the filter columns (project, matter, type ancestors,
status, language, date, identifiers), and the embedding model that produced
its vector.

## The service registry

`GET /api/components` (admin) returns one row per backing service and probes
each **api_url** live with a 2-second timeout:

| Role | Product | api_url (probed) | ui_url (browser link) |
| --- | --- | --- | --- |
| Model gateway | LiteLLM | `components.litellm_url` | same |
| Document parsing | Docling Serve | `components.docling_url` | n/a |
| Search index | OpenSearch | `components.opensearch_url` | same |
| Pipeline orchestrator | `components.orchestrator_provider` | `components.orchestrator_api_url` | `components.orchestrator_ui_url` |
| Traces | Langfuse | `components.traces_api_url` (falls back to `traces_url`) | `components.traces_url` |

Health semantics are reachability, not configuration: **any** HTTP answer,
including 401 or 404, counts as `ok`; a connection failure is
`unreachable`; an empty URL is `disabled`.

`api_url` and `ui_url` are two names for one service on purpose: LegalMemory
probes over the container network while a browser opens the published host
name. Using the public URL for both once made the Langfuse health check
resolve to the app container itself and report a running Langfuse as
unreachable.

The **Service links** toggle in the console topbar controls whether the
registry (and other pages) show deep links into the component dashboards
(Hatchet, OpenSearch, Langfuse, LiteLLM) and the API docs. It is off by
default and stored per browser.

## Configuration reference

Precedence is `environment > saved file > defaults`. Any key can be pinned by
its environment variable (`KI_` prefix, `__` as the nesting delimiter); a
console save that would change a pinned setting is refused with the exact
variable named, and `GET /api/config/precedence` reports which source owns
each value.

### Model assignments

| Key | Env var | Default | Effect |
| --- | --- | --- | --- |
| `pipeline.stages.<stage>.model` | n/a (part of the `stages` map) | `$KI_LLM_MODEL` | Gateway model the stage calls. |
| `retrieval.embedding_model` | `KI_RETRIEVAL__EMBEDDING_MODEL` | `$KI_EMBEDDING_MODEL` | The appliance-wide embedding model; locked while chunks exist. |
| `retrieval.rerank_model` | `KI_RETRIEVAL__RERANK_MODEL` | `$KI_LLM_MODEL` | Scores the top collapsed hits when rerank is enabled. |
| `ask_model` | `KI_ASK_MODEL` | `$KI_LLM_MODEL` | The reference `/api/ask` assistant. |

### `components.*`

| Key | Env var | Default | Effect |
| --- | --- | --- | --- |
| `components.litellm_url` | `KI_COMPONENTS__LITELLM_URL` | `http://litellm:4000` | Model gateway; the base URL for every model call and for the admin model registry. |
| `components.docling_url` | `KI_COMPONENTS__DOCLING_URL` | `http://docling:5001` | Document conversion service. |
| `components.opensearch_url` | `KI_COMPONENTS__OPENSEARCH_URL` | `http://opensearch:9200` | Search index. |
| `components.orchestrator_provider` | `KI_COMPONENTS__ORCHESTRATOR_PROVIDER` | `hatchet` | `local` (in-process runner) or `hatchet` (durable workers). |
| `components.orchestrator_api_url` | `KI_COMPONENTS__ORCHESTRATOR_API_URL` | *(empty)* | Orchestrator engine URL; empty shows the registry row as `disabled`. |
| `components.orchestrator_ui_url` | `KI_COMPONENTS__ORCHESTRATOR_UI_URL` | *(empty)* | Orchestrator dashboard link. |
| `components.traces_api_url` | `KI_COMPONENTS__TRACES_API_URL` | `http://langfuse:3000` | Trace store, probed over the container network. |
| `components.traces_url` | `KI_COMPONENTS__TRACES_URL` | `http://localhost:3001` | Trace store as a browser opens it. |
| `components.docs_url` | `KI_COMPONENTS__DOCS_URL` | *(empty)* | Base URL of the hosted documentation; empty hides the doc links in the console. |
