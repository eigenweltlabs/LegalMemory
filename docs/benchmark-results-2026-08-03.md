# Benchmark run — 2026-08-03, harvey-full (14,050 documents)

First full run of the unified benchmark against the live 15k-document index on
`ki-pipeline`. Corpus: Harvey LAB packed as a German firm's DMS, ingested through the
normal pipeline (13,544 documents / 1,251 matters / 742k chunks, 16 practice-area ACL
groups). Gold: `harvey-full`, 875 machine-verified labels from 300 stratified
scenarios. Model: `qwen3.6-35b-a3b` on a spot vLLM fleet; embeddings
`text-embedding-3-small`.

**Read this section first.** Two adversarial audits of the raw results found eleven
benchmark defects and four product defects. Everything below is post-correction, and
the retracted claims are listed explicitly — they were wrong, and the reasons are
instructive.

## 1. What the benchmark establishes

### Agentic tier (the headline: agents consuming the RAG)

200 queries × 6 configs, paired, exact McNemar against the shipped config.

| config | success (95% CI) | questions | known-item | any-gold | ctx recall | vs full_tools | tool calls |
|---|---|---|---|---|---|---|---|
| `classic_rag` (one-shot, excerpt-only) | 0.585 [0.516, 0.651] | 0.45 | 0.72 | 0.910 | 0.758 | −60 net, **p<0.001** | 1.0 |
| `agent_naive` (agent + dense-only) | 0.710 [0.644, 0.769] | 0.89 | **0.53** | 0.745 | 0.542 | −35 net, **p<0.001** | 3.83 |
| `agent_hybrid` (agent + generic hybrid) | 0.920 [0.874, 0.950] | 0.94 | 0.90 | 0.920 | 0.741 | +7, p=0.19 — tie | 2.40 |
| `agent_full_search` (ours, search only) | 0.900 [0.851, 0.934] | 0.93 | 0.87 | 0.935 | 0.766 | +3, p=0.63 — tie | 2.85 |
| `agent_full_filters` (ours + filters) | 0.910 [0.862, 0.942] | 0.93 | 0.89 | 0.925 | 0.759 | +5, p=0.27 — tie | 2.91 |
| `agent_full_tools` (shipped) | 0.885 [0.833, 0.922] | 0.91 | 0.86 | **0.940** | **0.805** | reference | 3.01 |

**Defensible claims:**

1. **An agent loop over lexical-capable retrieval is decisively better than either
   baseline.** vs `agent_naive` (dense-only): +19 points, p<0.001. vs one-shot
   `classic_rag`: +32 points, p<0.001. Both are well-powered, paired results.
2. **The lexical/identifier legs are what make known-item lookup work.** Dense-only
   scores 0.53; every lexical-capable config scores 0.86–0.90. A lawyer pasting a
   reference number is the single clearest win in the benchmark.
3. **The agent loop is what makes question answering work**: 0.45 one-shot → 0.89–0.94
   agentic. But see the caveat below — this gap is *not* purely about agency.

**Not resolvable at n=200:** every difference among the four hybrid/full configs
(all p ≥ 0.19). The benchmark cannot detect differences below ~4–5 points here.
Do not rank them.

### Retrieval tier (single-shot diagnostic, 875 queries × 13 presets)

Reported per gold kind — **never pooled**, because `known_item` gold is defined as
"documents containing this string", which a lexical retriever reproduces by
construction.

| preset | nDCG@10 questions | nDCG@10 known-item | recall@10 |
|---|---|---|---|
| **`full_no_statusboost`** | **0.639** | 0.652 | 0.782 |
| `full_rrfk20` | 0.603 | 0.636 | 0.775 |
| `full_no_identifier` | 0.603 | 0.572 | 0.731 |
| `hybrid_rrf` | 0.598 | 0.589 | 0.724 |
| `full` (shipped) | 0.586 | 0.589 | 0.735 |
| `bm25` | 0.574 | **0.750** | 0.815 |
| `full_no_clauses` | 0.583 | 0.586 | 0.733 |
| `full_no_profiles` | 0.581 | 0.591 | 0.734 |
| `full_no_collapse` | 0.502 | 0.508 | 0.627 |
| `full_no_semantic` | 0.462 | 0.625 | 0.727 |
| `full_no_lexical` | 0.468 | 0.359 | 0.542 |
| `naive_dense` | 0.549 | **0.277** | 0.501 |

**Interpretation ceiling:** gold averages 2.49 documents per query (68% have ≥2),
so a system returning *the* answering document at rank 1 and nothing else scores
**~0.64**, not 1.0. `full`'s 0.586 is ~91% of that ceiling, not 59% of anything.

## 2. Product findings (ranked by value)

1. **The version-status boost is net-negative.** Disabling it gains **+0.059 pooled
   [+0.048, +0.070]** and **+0.053 on questions alone** — significant, robust across
   every stratum, metric and cutoff. Mechanism: it is a *multiplier* on fused RRF
   scores with range 1.2/0.7 = 1.71×, while the RRF dynamic range across 20 positions
   is only 1.32× — so the boost can outrank a full 20-position retrieval gap.
   **Fix: apply it as a post-collapse tie-break, or cut its range to ≤1.05/0.95.**
2. **The reranker was 100% broken.** `_rerank` called the gateway without
   `max_output_tokens`, so a reasoning model exhausted the default budget on hidden
   reasoning and returned *empty content on every call* — measured 5/5 schema-invalid
   failures. `rerank_enabled=True` never worked on this deployment; it went unnoticed
   because the shipped default is `false`. Fixed (budget raised to 8000, verified
   3/3 succeeding). **Separate concern: a rerank call costs ~28s**, so even working,
   it is unusable on the interactive path at this model size.
3. **Disabled retrieval legs were not disabled.** A leg with weight 0 was still
   executed and still inserted candidates at score 0.0, which sorted last but filled
   result slots. An operator setting `weight_lexical=0` still got lexical hits. Fixed
   — and the effect was large: `naive_dense`'s known-item nDCG fell 0.577 → 0.277
   once it stopped receiving free lexical candidates.
4. **The candidate pool was as shallow as the answer.** Each leg fetched exactly the
   requested result count, so fusion, status boost, collapse and rerank could only
   reorder what one leg already had in its own top-N — and a document indexed as
   body+profile+clause spent three slots on one document. Now 10× (`candidate_pool_factor`).
5. **The identifier leg earns nothing** (−0.001 pooled, CI spans zero) despite
   carrying the highest weight in the config (1.5). Consistent with the agentic
   trajectories: the `identifier=` exact filter returned **zero results on 98% of
   calls** (123/126), and every failing example is a punctuated or prefixed value
   (`ein: 93-2058471`, `no. 18742`) — an ingest-vs-query normalization defect.
   Agents mask it by retrying unfiltered; a non-agentic client just gets nothing.
6. **Profile and clause embeddings are not measurable here** (−0.001 and −0.003).
   Not evidence they are worthless — the gold has no queries that specifically need
   clause-level or matter-level abstraction.
7. **`fusion_rrf_k=60` is not optimal for this corpus**: k=20 gains +0.034, k=240
   loses 0.085. Note this is entangled with finding 1 — a smaller k widens the RRF
   range and partially neutralizes the status-boost multiplier.
8. **Search cost**: ~1.0s per query end-to-end after the perf fix (was ~8s), of which
   the query embedding is a fixed synchronous cost every preset pays, including
   lexical-only ones (`bm25` runs in 0.35s now that it skips the embedding).

## 3. Retracted claims

These were reported during the run and are **wrong**:

- ~~"BM25 beats the full system by +0.08 nDCG@10."~~ 104% of that delta came from the
  415 queries (47%) where the query *literally is* its own gold answer string. On the
  other 460, the difference is −0.006 with a CI spanning zero; on questions only BM25
  does not win, and against `full_no_statusboost` it loses by 0.065 [−0.088, −0.042].
- ~~"Generic hybrid beats our full system in the agentic tier."~~ Statistical noise
  (p=0.19), amplified by a scoring bug that made our own citation-shaped tool results
  invisible to the scorer.
- ~~"The full tool suite hurts known-item performance."~~ Artifact of the same bug:
  7 confirmed runs where the agent named the gold document verbatim and scored zero.
- ~~"Ethical walls clean."~~ Never probed — the flag was fail-open. Wall probing is
  now opt-in (`--check-walls`) and reports `None` when it did not run.
- ~~Any cost-per-query figure.~~ Derived from a workspace-wide spend counter that
  cannot attribute concurrent traffic; two configs once reported identical spend to
  six decimals while consuming 8.7M and 10.3M tokens. Cost is now reported as tokens.
- ~~Any latency comparison between presets~~ from the multi-process runs.

## 4. Benchmark defects fixed during the run

Scoring: citations-shaped evidence invisible to path tracking; known-item success
was set-membership (an agent could answer "not found" and score a success);
`answer_in_context` counted excerpt windows per chunk, ranking a strictly worse
config above `full`; `classic_rag` reported `len(hits)` as its tool-call count.
Statistics: bootstrap replaced by exact McNemar for paired binary outcomes, Wilson
intervals on every rate, and the table now prints "not resolvable" instead of
ranking noise. Robustness: per-preset/per-config checkpointing (a crash used to lose
the entire run), per-query retry, gateway 5xx retry, and query-level fault isolation.

## 5. Known limitations of this benchmark

1. **Gold is 47% tautological for lexical retrieval** — `known_item` queries are the
   answer string. The fix is to require paraphrased or partial values at generation.
2. **Relevance means "contains the string", not "answers the question"**, so 68% of
   queries have 2–5 "relevant" documents and the achievable nDCG ceiling is ~0.64.
   Split gold into primary (the answering document) and secondary co-mentions.
3. **n=200 in the agentic tier** resolves nothing below ~4–5 points. ~1,500 paired
   queries, or 3 seeds × 200, would be needed to separate the top configs.
4. **Queries cluster by scenario** (875 queries from 293 scenarios sharing document
   bundles); a scenario-clustered bootstrap widens CIs by up to 1.23× (no conclusion
   flipped, but it should be adopted).
5. **~10–15% of question gold is contestable** — verified cases where the agent read
   the document more carefully than the gold, and under-specified queries (two
   different "Greenfield" entities in the corpus).
6. **English corpus**: Harvey is US-law. The product's reranker prompt is German —
   fine in production, a mismatch here. German OCR/identifier behavior is untested by
   this benchmark; `verify-fixtures` remains the language/jurisdiction harness.
7. **`full` = no rerank, no graph RAG** — both ship disabled and were not exercised.

## 6. Recommended next steps

**Product:** make the status boost a tie-break (biggest single quality win available);
fix identifier normalization; decide whether the reranker belongs on the interactive
path at 28s/call; consider `fusion_rrf_k=20`.

**Benchmark:** paraphrase known-item queries; primary/secondary graded gold; scale
the agentic tier to ~1,500 queries or 3 seeds; add queries that exercise
traverse/relations/ontology so the full-tool-suite row means something.

## Reproduce

```bash
ki generate-gold /testdata/harvey-full --limit-scenarios 300 --per-scenario 4 \
  --max-gold-docs 5 --seed 42 --concurrency 12 --output-dir /data/benchmark
ki freeze-gold <corpus> --name harvey-full
ki run-retrieval-eval harvey-full --presets all --concurrency 4 --checkpoint-dir <dir>
ki run-agentic-eval harvey-full --configs default --limit 200 --concurrency 16 \
  --max-steps 20 --checkpoint-dir <dir>
```
