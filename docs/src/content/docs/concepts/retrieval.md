---
title: How retrieval works
description: "The retrieval query path in LegalMemory: access-scope compilation, the fused search legs, ranking adjustments, index contents, and configuration."
---

LegalMemory answers search queries from two stores. PostgreSQL holds the structured layer (documents, versions, matters, relations, grants) and is the authority for authorization. OpenSearch holds one chunk index and performs lexical (BM25) and dense (approximate kNN) ranking. The retrieval service (`RetrievalService` in `src/knowledge_index/retrieval.py`) compiles an access scope in SQL, runs the ranked legs inside that scope, fuses and adjusts the results, and re-verifies every returned row against SQL before it leaves the service.

## Query path

A semantic query passes through these steps in order:

1. Compile the access scope in SQL (before any ranking).
2. Embed the query text (one synchronous embedding call).
3. Run the ranked legs in a single OpenSearch `_msearch` request.
4. Fuse the legs with reciprocal rank fusion (RRF).
5. Multiply each candidate's score by its version-status boost.
6. Re-verify each candidate against SQL and collapse to one result per document.
7. Optionally rerank the top candidates with an LLM.
8. Return the top `limit` hits.

### Scope compilation

`AccessService.compile_scope` (`src/knowledge_index/permissions.py`) runs before any lexical or vector score is calculated. Retrieval never fetches a global nearest-neighbour set and filters afterwards; the ACL decision is made in SQL and embedded into every OpenSearch query as a strict filter.

The scope is built from `version_predicate`, a correlated SQL predicate over `Document` and `DocumentVersion` rows:

- The caller's principals are normalized (trimmed, casefolded), passed through configured aliases, and expanded with group memberships mirrored from the sources. Without this expansion, a mirrored grant to a source group would match nobody.
- Deny wins at every scope: a matching deny grant on the project, the document, or a source object excludes the version.
- An allow must come from a project grant, a document grant, or a mirrored source-object grant. With `security.source_acl_mode: sufficient` (default) a source allow suffices; with `intersect` a local project/document allow is additionally required.
- If a source object carries a known ACL, the version needs at least one accessible active source observation; a local grant cannot override an external source ACL. Sources of kind `local_fs` with no object ACL delegate to the project boundary. External sources with no mirrored grants fail closed.
- Administrators (`role:admin`, `system:admin`) bypass grants but still require an active (non-deleted) source observation.

`compile_scope` evaluates this predicate to the concrete sets of visible project ids and document ids (intersected with any requested `project_id` filter) and returns a `CompiledAccessScope`. Its `opensearch_filter()` is a `bool` filter with `terms` clauses on `document_id` and `project_id`, the exact ids the SQL authorization query produced. An empty document set compiles to `match_none`, so an unauthorized caller produces empty queries, not unfiltered ones.

Metadata filters (`SearchFilters` in `src/knowledge_index/retrieval_types.py`) are appended to the same `bool` filter:

| Filter | Match |
| --- | --- |
| `project_id` | `term` on `project_id` (also narrows the compiled scope) |
| `matter_id` | `term` on `matter_id` |
| `doc_type` | `term` on `doc_type_ancestors`, with subtree semantics: filtering by an interior ontology node matches every document typed at or below it |
| `version_status` | `term` on `version_status` |
| `language` | `term` on `language` |
| `date_from` / `date_to` | `range` on `doc_date` |
| `clause_type` | `term` on `clause_type` (only clause rows carry it) |
| `chunk_kind` | `term` on `chunk_kind` (`chunk`, `profile`, or `clause`) |

### Retrieval legs

Three ranked legs run over the chunk index (`OpenSearchIndex.multi_search` in `src/knowledge_index/search_backend.py`). Each leg carries the same strict filter, so fusion never sees an unauthorized row.

| Leg | Query | Field |
| --- | --- | --- |
| `lexical` | `match` on the query text | `text` (BM25, `german` analyzer) |
| `semantic` | `knn` on the query embedding, with the strict filter passed as the kNN `filter` (pre-filtered approximate kNN) | `embedding` (HNSW) |
| `identifier` | `match` on the query text | `identifiers_text`, the space-joined identifiers extracted for the document at ingest, so a pasted case number, Aktenzeichen, or statute reference matches the document that carries it. The query is not parsed with regexes |

Each leg requests an oversampled window of `min(max(limit * 5, 50), 500)` hits.

Decision records are not one of the fused legs. `search_decisions` is a separate method (and MCP tool) that scores `DecisionRecord` rows in SQL by token overlap between the query and the record's locus, change summary, and rationale text, after checking each record's evidence sources against the caller's principals.

### `_msearch` fusion

All active legs are sent in one `_msearch` round-trip. A leg that cannot match is skipped without a network hop: the `lexical` and `identifier` bodies are omitted when the compiled scope is `match_none`, and the `identifier` body also when the query is blank (the `semantic` body is always sent; a `match_none` filter makes it return nothing). A per-leg OpenSearch error raises a `RuntimeError`; nothing degrades silently.

Results are fused with reciprocal rank fusion, aggregated by chunk id:

```
score(chunk) += weight_leg / (fusion_rrf_k + rank)
```

with `rank` starting at 0 within each leg. Defaults: `fusion_rrf_k = 60`, `weight_lexical = 1.0`, `weight_semantic = 1.0`, `weight_identifier = 1.5`.

For candidates that appear in the identifier leg, the service records `matched_identifiers`: the document's indexed identifiers whose casefolded text occurs as a substring of the query. This is reported on the hit; it does not change the score.

## Ranking adjustments

### Version-status boost

After fusion, each candidate's score is multiplied by `retrieval.version_status_boost[version_status]`. Ranking decays by supersession, not by age. Defaults:

| `version_status` | Multiplier |
| --- | --- |
| `executed` | 1.2 |
| `final` | 1.0 |
| `unknown` | 0.8 |
| `draft` | 0.7 |

A status not present in the map gets a multiplier of 1.0. This is a ranking adjustment; the `version_status` filter remains available for hard exclusion.

### SQL re-verification

Every candidate is materialized through SQL before it can be returned: the document and version rows must exist and match, and the version must have at least one authorized source object (checked with `version_predicate` plus per-source grant evaluation). Candidates that fail drop out. The OpenSearch filter is a projection of the SQL decision; SQL remains the authoritative backstop, and the re-verify also builds each hit's citation (project, document, version, source objects, matched chunk).

### Document collapse

With `retrieval.collapse_per_document: true` (default), surviving candidates are grouped by `document_id` and one hit per document is returned: the group is sorted by fused score, with "is the document's `latest_final_version_id`" as the tiebreaker, and the top entry wins. Other versions and chunks of the same document are dropped from the result list (they stay reachable through `get_document` and graph traversal). With collapse disabled, up to `retrieval.max_chunks_per_document` chunks per document are returned instead.

### Optional reranker

With `retrieval.rerank_enabled: true`, the top 20 collapsed hits are sent in one call to the model assigned as `retrieval.rerank_model`, which returns a relevance score from 0 to 10 per version id. Those scores replace the fused scores and determine the final order; hits the model does not score are dropped. On a gateway error the call raises; there is no silent fallback to the fused order. When the reranker is disabled (the default), the fused, boosted, collapsed order is returned directly and no LLM call happens on the query path; the query embedding is then the only synchronous model call.

### Metadata-only search

`search_filter` (no query text) skips the legs entirely: one filter-only query sorted by `doc_date` descending, deduplicated to one hit per version, with score 0. Order is deterministic; no embedding call is made.

## What is in the index

The pipeline's index stage (`_index` in `src/knowledge_index/pipeline/runner.py`) writes one OpenSearch document per chunk row. Three kinds of rows exist per document version:

- **Body chunks** (ordinals 0..n, `meta.kind = "chunk"`): the converted text split into windows of `chunk_chars` characters with `chunk_overlap_chars` overlap, preferring newline boundaries.
- **One profile row** (ordinal −1, `meta.kind = "profile"`): written only for the document's latest final version when `profile_embeddings` is on. Deterministically assembled (no LLM, no regex) from labeled lines (title, document type, matter, reference numbers, parties, identifiers, date) plus the first 400 characters of the text.
- **Clause rows** (ordinals 1000+, `meta.kind = "clause"`): written for final/executed versions when `clause_embeddings` is on. Text and locus come from the model-extracted `notable_clauses` artifact of the metadata stage; each row carries its `clause_type` ontology node.

The stored `text` is always the raw chunk text. When `chunk_contextualize` is on, the *embedded* string is the chunk text prefixed with a one-line context header joining the title, the document type label and the matter title, so an isolated paragraph stays findable; the header is not stored or displayed.

Fields per indexed chunk (`OpenSearchIndex._doc_body`):

| Field | Type | Content |
| --- | --- | --- |
| `text` | `text` (`german` analyzer) | Raw chunk text (BM25 target, excerpt source) |
| `embedding` | `knn_vector` (HNSW) | Embedding of the context-prefixed text; excluded from search responses |
| `project_id`, `document_id`, `document_version_id`, `matter_id` | `keyword` | Identity and filter columns |
| `doc_type`, `doc_type_ancestors` | `keyword` | Ontology node id and its ancestor closure (subtree filtering) |
| `version_status`, `language`, `doc_date` | `keyword` / `date` | Filter and boost inputs |
| `chunk_kind`, `clause_type` | `keyword` | Row kind (`chunk` / `profile` / `clause`) and clause facet node |
| `identifiers`, `identifiers_text` | `keyword` / `text` | Model-extracted document identifiers (case numbers, statute references, registry numbers), set on the document during the metadata stage and copied to every chunk; the identifier leg matches `identifiers_text` |
| `allowed_principals`, `denied_principals`, `access_version` | `keyword` / `integer` | Denormalized projection of the effective grants at index time, for inspection and export only. Query-time authorization uses the compiled scope from SQL, not these fields |
| `meta` | `object`, not indexed | Chunk payload: `source_object_id`, `kind`, and for clause rows `locus` and `clause_type` |

Index mechanics:

- The mapping is `dynamic: false`; missing mapped fields are added additively to live indices at startup, but documents indexed before a field existed need a re-sync to become searchable on it.
- The `embedding` field is HNSW with `vector_engine` (default `lucene`, which supports pre-filtered kNN), `vector_space_type` (default `cosinesimil`), `hnsw_m`, and `hnsw_ef_construction`. On startup the live mapping's dimension is checked against `embedding_dimensions`; a mismatch raises with an instruction to reindex, since vectors from two embedding models cannot share one ANN index. The reindex action rebinds `index_name` to a name derived from the embedding model and dimension.
- Writes go through a single `_bulk` code path; partial failures raise with the failed item ids.
- When only a source ACL changed, an access-only reindex updates the principal projection fields and increments `access_version` without re-splitting or re-embedding anything.

## Configuration

All fields live under `retrieval.*` in `config.json` and can be set with environment variables `KI_RETRIEVAL__<FIELD>` (nested delimiter `__`; the boost map is JSON).

| Key | Env var | Default | Effect |
| --- | --- | --- | --- |
| `index_name` | `KI_RETRIEVAL__INDEX_NAME` | `knowledge-index-chunks-v1` | OpenSearch index read and written; the reindex action switches it to the embedding-signature-derived name |
| `embedding_dimensions` | `KI_RETRIEVAL__EMBEDDING_DIMENSIONS` | `1536` | Dimension of the `knn_vector` field; verified against the live index at startup |
| `vector_engine` | `KI_RETRIEVAL__VECTOR_ENGINE` | `lucene` | HNSW engine (`lucene`, `faiss`, `nmslib`) |
| `vector_space_type` | `KI_RETRIEVAL__VECTOR_SPACE_TYPE` | `cosinesimil` | kNN similarity space |
| `hnsw_m` | `KI_RETRIEVAL__HNSW_M` | `16` | HNSW graph fan-out, applied at index creation |
| `hnsw_ef_construction` | `KI_RETRIEVAL__HNSW_EF_CONSTRUCTION` | `128` | HNSW build-time beam width, applied at index creation |
| `chunk_chars` | `KI_RETRIEVAL__CHUNK_CHARS` | `1200` | Body chunk window size (characters) |
| `chunk_overlap_chars` | `KI_RETRIEVAL__CHUNK_OVERLAP_CHARS` | `120` | Overlap between consecutive body chunks |
| `chunk_contextualize` | `KI_RETRIEVAL__CHUNK_CONTEXTUALIZE` | `true` | Prefix the context header to the embedded string (stored text stays raw) |
| `profile_embeddings` | `KI_RETRIEVAL__PROFILE_EMBEDDINGS` | `true` | Index one profile row per document (latest final version) |
| `clause_embeddings` | `KI_RETRIEVAL__CLAUSE_EMBEDDINGS` | `true` | Index clause rows for final/executed versions |
| `fusion_rrf_k` | `KI_RETRIEVAL__FUSION_RRF_K` | `60` | RRF rank constant |
| `weight_lexical` | `KI_RETRIEVAL__WEIGHT_LEXICAL` | `1.0` | RRF weight of the lexical leg |
| `weight_semantic` | `KI_RETRIEVAL__WEIGHT_SEMANTIC` | `1.0` | RRF weight of the semantic leg |
| `weight_identifier` | `KI_RETRIEVAL__WEIGHT_IDENTIFIER` | `1.5` | RRF weight of the identifier leg |
| `weight_decisions` | `KI_RETRIEVAL__WEIGHT_DECISIONS` | `0.8` | Declared in config; not read by the current query path (`search_decisions` scores by term overlap) |
| `version_status_boost` | `KI_RETRIEVAL__VERSION_STATUS_BOOST` | `{"executed": 1.2, "final": 1.0, "unknown": 0.8, "draft": 0.7}` | Post-fusion score multiplier per version status; unlisted statuses get 1.0 |
| `collapse_per_document` | `KI_RETRIEVAL__COLLAPSE_PER_DOCUMENT` | `true` | Return one hit per logical document |
| `max_chunks_per_document` | `KI_RETRIEVAL__MAX_CHUNKS_PER_DOCUMENT` | `3` | Per-document chunk cap when collapse is off |
| `rerank_enabled` | `KI_RETRIEVAL__RERANK_ENABLED` | `false` | LLM rerank of the top 20 collapsed hits with `retrieval.rerank_model` |
| `graph_rag_enabled` | `KI_RETRIEVAL__GRAPH_RAG_ENABLED` | `false` | Declared in config; not read by the current retrieval code |

## Relationship to the structured layer

The [pipeline](/product/pipeline/) builds the structured layer that retrieval leans on: version chains determine each document's `latest_final_version_id`, which document collapse uses as its tiebreaker, and the `doc_type_ancestors` closure that powers subtree filtering comes from the ontology classification. Relations (`supersedes`, `annex_of`, `responds_to`, `references`, thread membership) are not consulted during ranking; they are exposed separately through the `traverse` and related-documents MCP tools, where every returned entity is independently authorization-checked and resolved back to citable source observations. The entities and edges themselves are described in the [data model](/concepts/data-model/).

## Failure behaviour

- If OpenSearch is unreachable or returns an error, the search call raises (HTTP errors propagate; a failed `_msearch` leg raises `RuntimeError`). There is no degraded or SQL-only fallback for chunk search.
- An empty compiled scope produces empty results, not an error: authorization is fail-closed at every layer (scope compilation, per-leg filters, SQL re-verification).
- Reranker enabled + gateway error: the query fails rather than silently returning the un-reranked order.
- A partial bulk indexing failure raises with the failed chunk ids; index writes do not degrade silently.
