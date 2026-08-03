# Benchmarking — design

How we measure the system's quality end-to-end: **does the whole chain — insertion
through retrieval through an agent — get the right answer?** One benchmark system,
two tiers over one corpus, one gold set, one index. For pipeline *correctness* see
`verify-fixtures`; for throughput/latency/cost see `scale-testing.md`.

## The shape

```
generate-benchmark → generate-gold → freeze-gold → bench-ingest → run-*-eval
     corpus            LLM + verify     commit        embed          score
```

- **Corpus**: Harvey LAB task bundles packed into a DMS tree (`generate-benchmark`),
  ingested through the normal pipeline. Nothing benchmark-specific in the ingest path.
- **Gold**: derived from the **source files, never the database** — so a conversion,
  extraction, classification, versioning, ACL, or indexing failure surfaces as a
  retrieval miss. Gold can never inherit an insertion mistake as ground truth.
- **Evaluation**: two tiers, below.

## Gold — one generation pipeline, two kinds, no humans

`ki generate-gold` runs one LLM pass per (stratified-sampled) scenario and emits two
label kinds:

| kind | query shape | exercises |
|---|---|---|
| `question` | natural lawyer question ("what cross-default threshold did we agree on the Meridian ISDA?") | semantic retrieval, profiles, contextualization |
| `known_item` | a pasted identifier/value (party name, charter number, case number, amount) | lexical + identifier legs, exact-match behavior |

The LLM only **proposes** `(kind, query, answer, source document)`. Three mechanical
checks decide what becomes gold — there is no human review:

1. **Verbatim verification** — the answer must appear as a substring of a bundle
   source document, or the proposal is dropped. Hallucinated gold is impossible; the
   model's document guess is a hint, never trusted.
2. **Corpus-wide discrimination** — the answer is searched across *every* document
   the query's principal can see (the whole practice-area scope, not just the
   bundle). Gold becomes exactly the documents that contain it; a value present in
   more than `--max-gold-docs` visible documents identifies nothing and is dropped.
3. **Dedupe** by `(kind, normalized query)` — re-runs are safe and incremental.

Rejections are logged to `rejected.jsonl` with diagnosed reasons (hallucinated,
reformatted, unreadable source, not discriminative) — nothing is silently discarded.
The result is frozen (`ki freeze-gold`) into `benchmark/data/` and committed; every
eval reads the frozen set and **fails fast** if the ingested corpus doesn't fully
cover it — a partial corpus yields meaningless numbers, so it refuses to score.

## Tier 1 — agentic matrix (the headline)

Retrieval is consumed by agents, so the number that matters is measured that way:
`ki run-agentic-eval` runs each gold query through agent configs — (retrieval
preset, tool allowlist, mode) triples over the real MCP surface:

| config | retrieval | tools | answers |
|---|---|---|---|
| `classic_rag` | full | none (top-k stuffed) | is an agent worth it at all |
| `agent_naive` | naive_dense | search only | agentic RAG on a generic vector store |
| `agent_hybrid` | hybrid_rrf | search only | agentic on the best generic retriever |
| `agent_full_search` | full | search only | our retrieval, minimal agency |
| `agent_full_filters` | full | + search_filter | do filters earn their place |
| `agent_full_tools` | full | everything | **the shipped system** |

Scoring per kind: `question` → LLM equivalence judge against the verified gold
answer; `known_item` → deterministic (success iff a gold document was surfaced — the
task *is* finding the document). Both report context recall, tool calls, tokens,
wall time, and real LiteLLM spend. Per-query success feeds a paired bootstrap
against `agent_full_tools`, and a **wall probe** runs the full-tools agent under an
outsider principal — an agent with tools probing the walls is a stronger security
test than a raw query.

## Tier 2 — single-shot retrieval matrix (the microscope + CI gate)

`ki run-retrieval-eval` scores the gold as ranked lists through `RetrievalService`
under query-time presets — deterministic, zero-LLM (except rerank presets), minutes
per matrix. Two preset families (`presets.py`):

- **Competitors** — the standard RAG ladder: `bm25` → `naive_dense` →
  `naive_dense_rerank` → `hybrid_rrf` → `hybrid_rrf_rerank` → `full`. Generic
  presets run flat (no identifier leg, no supersession boost, no collapse) and see
  **body chunks only** — profile/clause rows are our ingest features and would
  otherwise credit the baseline. `hybrid_rrf_rerank` is the strongest off-the-shelf
  pipeline and the baseline `full` has to beat.
- **Ablations** — leave-one-out from `full`: `full_no_identifier`, `full_no_lexical`,
  `full_no_semantic`, `full_no_collapse`, `full_no_statusboost`, `full_no_profiles`,
  `full_no_clauses`, `full_rerank`, plus an RRF-k sweep (`full_rrfk20/240`). The
  per-kind breakdown is the payoff: dropping the identifier leg should tank
  `known_item` while leaving `question` flat.

Metrics: recall@{1,5,10,20}, MRR, nDCG@10 (overall + per kind), latency percentiles,
rank-1 version-status mix, per-preset wall probe, and a paired-bootstrap 95% CI on
per-query nDCG@10 vs `full` — every row reads "delta vs full, with error bars".

**Gate** (CI): `full` must beat `naive_dense` on nDCG@10 by `--min-lift`, the lift
must be bootstrap-significant, every preset must leak nothing across ethical walls,
and corpus coverage must be full.

**Bridge rule**: any ablation whose Tier-2 delta is significant beyond its CI earns a
confirmation run in Tier 1 (the same feature removed under `agent_full_tools`) — so
the final claim is "feature X is worth so-much nDCG *and* so-much end-task success".

## Known limits (deliberate)

- `chunk_contextualize` can't be ablated query-time (headers are baked into embedded
  chunk text); testing it means re-embedding — out of scope for the matrix.
- All presets share our embedding model and chunking: the matrix isolates
  *architecture*, not embedder choice.
- Learned-sparse / late-interaction / RAPTOR / GraphRAG baselines need different
  index infrastructure and are intentionally absent.
- Harvey is US-law/English: it stresses retrieval mechanics and scale, not German
  OCR or identifiers. The German fixture + `verify-fixtures` remains the
  language/jurisdiction harness.
