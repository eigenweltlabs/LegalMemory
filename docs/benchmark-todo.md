# Benchmark & retrieval — findings status

Every item from the 2026-08-03 first full run and its two adversarial audits, with
status. `DONE` items carry the commit that fixed them and are live in the benchmark
that produced `docs/benchmark-results-2026-08-03.md`; `OPEN` items are not.

Legend: **DONE** = fixed and committed · **OPEN** = not started · **PARTIAL** = fixed
for one path, gap remains.

---

## A. Product — retrieval quality

| # | Item | Status | Ref |
|---|---|---|---|
| A0 | **Query-shape routing** — `full` collapses on pasted values: addresses 0.306 vs bm25 0.810, registry ids 0.412 vs 0.555, entity names 0.543 vs 0.763, while prose questions are a dead heat (0.590 vs 0.581). Mechanism: for a pasted string the lexical leg is near-oracle, then RRF destroys it — at k=60 any two-leg document within 60 positions beats the correct document at lexical rank 0, and the semantic leg reliably supplies that second leg to ~100 topically-similar wrong documents. Route pasted-value queries to lexical-dominant retrieval (+`match_phrase`, flat boost, k=20). **Largest single gain available: ~+0.11 overall.** | **OPEN** | — |
| A1 | **Version-status boost is net-negative** (−0.057 nDCG; −0.164 on all-draft-gold queries, 43 wins : 2 losses). Supersession is a *within-chain* concept implemented as a *cross-document* multiplier; at k=60 over a 100-deep pool it overrides **26 pool positions** for drafts. Decisive evidence it is not a labelling disagreement: `answer_in_context` — which never reads `gold_paths` — is worse with the boost at every cutoff. Fix: flat on the fusion path, status ordering inside the version chain at collapse (where `latest_final` already exists), optional post-collapse tie-break ≤1.05. | **OPEN** | — |
| A2 | **Identifier filter is exact-match on a raw keyword field.** Root cause: `identifiers` is `{"type":"keyword"}` matched with a case-sensitive whole-value `term` query; the extractor stores `LF-2024-0917`, agents type `lf-2024-0917` or a fragment → **200 of 205 agentic calls returned zero**. Fix: normalizer (lowercase, strip `.`/`-`/space) on the field *and* the query value, with a `match_phrase` fallback on `identifiers_text`. | **OPEN** | — |
| A2b | **Gate the identifier and semantic legs by query shape.** The identifier leg fires unconditionally at weight 1.5 (the highest): it is worth −0.053 on identifier-shaped queries but +0.013 on prose. The semantic leg is worth +0.119 on questions but −0.039 on known-item. Subsumed by A0 if A0 ships. | **OPEN** | — |
| A2c | **`text` is indexed with a German analyzer on an English corpus** (`search_backend.py:308`) while `identifiers_text` uses the standard analyzer. Affects every config equally so it explains none of the measured deltas — but it is silently costing all of them. | **OPEN** | — |
| A3 | **Reranker returned EMPTY on every call** — no `max_output_tokens`, so a reasoning model spent the whole budget on hidden reasoning. `rerank_enabled=true` was 100% broken. | **DONE** | `166f23e` |
| A4 | **Reranker costs ~28s/query** even when working — cannot sit on the interactive path at this model size. Decide: off the hot path, or drop the option. | **OPEN** | — |
| A5 | **Zero-weight legs still populated the candidate pool** (scored 0.0, sorted last, filled slots). Disabling a leg did not disable it. | **DONE** | `af91fd6` |
| A6 | **Candidate pool was as shallow as the answer** — every ranking stage could only reshuffle one leg's top-N; body+profile+clause spent 3 slots on one document. Now `candidate_pool_factor=10`. ⚠ **Must ship together with A7**: a deeper pool contains far more two-leg documents, each still able to beat a lexical rank-0 hit from 60 positions back, so at k=60 it makes the pasted-value failure (A0) *worse*, not better. | **DONE** (⚠ see A7) | `af91fd6` |
| A7 | **`fusion_rrf_k` 60 → 20** is worth +0.032 — and it is not an independent knob: k is the gain control on both the status boost (override depth 26 → 9 positions) and leg-count dominance (60 → 20 positions). Complementary with A1, not a substitute: of `full`'s 79 zero-scoring queries, dropping the boost rescues 32 and k=20 rescues 30, but they overlap on only 17 — **union 45/79 (57%), from a two-line config change**. Better still: replace count-driven RRF with normalized-score fusion, which removes leg-count dominance instead of tuning around it. | **OPEN** | — |
| A8 | **Entity disambiguation**: with several same-named entities in scope the agent commits to the wrong matter instead of asking (the "Greenleaf" case). | **OPEN** | — |
| A9 | **Deny-all / lexical-only queries still pay a synchronous embed.** Partly addressed — a zero-weight semantic leg now skips the embedding (A5); an empty ACL scope still embeds. | **PARTIAL** | `af91fd6` |
| A10 | **Search latency** 8s → ~0.8s (batched ACL verification, lazy verify, scope compiled once). | **DONE** | `864ae8a` |

## B. Benchmark — gold quality

| # | Item | Status | Ref |
|---|---|---|---|
| B1 | **Relevance means "contains the answer string", not "answers the question"** — so 68% of queries carry 2–5 gold docs and the achievable nDCG@10 ceiling is ~0.64, not 1.0. Fix: graded gold, `source_hint` primary (1.0), co-mentions secondary (0.3). | **OPEN** | — |
| B2 | **47% of known-item queries ARE their own gold answer string** — a lexical retriever reproduces the gold's definition by construction. Fix: require paraphrases/partials at generation. | **OPEN** | — |
| B3 | **Bad labels**: 2 unanswerable items, ≥5 with wrong/incomplete paths, ~10–15% of question references contestable, ambiguous entity names (two "Greenfield"s). | **OPEN** | — |
| B4 | **`corpus_texts` keys paths on the first scenario that references them**, so a document shared across scenarios with different principals silently drops from the second one's gold (0.6% here, unbounded in principle). | **OPEN** | — |
| B5 | Gold is generated from **source files, never the database**, so insertion defects surface as retrieval misses. | **DONE** (by design) | `c648793` |
| B6 | Verification: verbatim answer check + corpus-wide discrimination + dedupe, with diagnosed rejections. | **DONE** | `c648793` |

## C. Benchmark — scoring correctness

| # | Item | Status | Ref |
|---|---|---|---|
| C1 | **`_track_paths` saw only search-hit shapes**, so `get_document`/`resolve_entity`/`traverse` results (the `citations[]` contract) recorded zero paths — zeroing runs where the agent named the gold document verbatim, and penalizing exactly the configs using the richer tool surface. | **DONE** | `b08486e` |
| C2 | **Known-item success was set-membership** — an agent could answer "not found" and score a success. Now requires surfacing *and* naming the document. | **DONE** | `b08486e` |
| C3 | **Cost came from a workspace-wide spend counter** — unattributable (two configs reported identical spend to 6dp on 8.7M vs 10.3M tokens). Now exact per-run tokens. | **DONE** | `b08486e` |
| C4 | **`classic_rag` reported `len(hits)` as tool calls** (9.85 vs its true 1.0). | **DONE** | `b08486e` |
| C5 | **`answer_in_context` counted excerpt windows per chunk**, ranking a strictly worse config above `full`. Deduped by document and demoted to a diagnostic. | **PARTIAL** — still matches the 320-char excerpt, not full chunk text | `af91fd6` |
| C6 | **Ethical-wall `clean: true` when nothing was probed** (fail-open). Now `None`, and the gate requires an explicit `True`. | **DONE** | `af91fd6` |
| C7 | **Wall probing made opt-in** (`--check-walls` / `--wall-probe`) — quality runs don't pay for access-management testing. | **DONE** | `c30aa6f` |

## D. Benchmark — statistics

| # | Item | Status | Ref |
|---|---|---|---|
| D1 | **Exact McNemar + Wilson intervals** on paired binary outcomes; the table prints "not resolvable" instead of ranking noise. | **DONE** | `b08486e` |
| D2 | **Report per gold kind, never pooled** — pooling hands a lexical baseline a tautological half of the benchmark. | **DONE** | `af91fd6` |
| D3 | **n=200 in the agentic tier resolves nothing below ~4–5 points.** Scale to ~1,500 paired queries, or 3 seeds × 200 to average out agent-loop stochasticity. | **OPEN** | — |
| D4 | **Scenario-clustered bootstrap** — 875 queries come from 293 scenarios sharing document bundles; CIs widen up to 1.23×. | **OPEN** | — |
| D5 | **Holm correction** — 12+ comparisons currently run uncorrected. | **OPEN** | — |
| D6 | **Missing per-query key silently scores 0** in the paired comparison; should raise. | **OPEN** | — |
| D7 | **Publish metric ceilings** — `precision@10` cannot exceed 0.249 and `recall@1` cannot exceed 0.556 by construction. Partly done: the table footer states the nDCG ceiling. | **PARTIAL** | `af91fd6` |
| D8 | **Drop the duplicate nDCG@1 column** (identical to precision@1). | **OPEN** | — |

## E. Benchmark — coverage & robustness

| # | Item | Status | Ref |
|---|---|---|---|
| E1 | **`classic_rag` mixes agency with a 17× context-budget difference** — it sees excerpts while agents read whole documents. Give it a comparable budget, or add a "one-shot full-document" row. Currently only documented. | **PARTIAL** | `b08486e` |
| E2 | **No gold exercises traverse / relations / billing / ontology**, so the full-tool-suite row rests on ~30 runs. | **OPEN** | — |
| E3 | **Report max-steps truncation as a distinct outcome** from "wrong". Partly done: a forced final answer now runs at the step limit. | **PARTIAL** | `9973902` |
| E4 | **The 3 rerank presets never completed** (13/16 retrieval presets). Low value — A4 disqualifies rerank on latency regardless. | **OPEN** | — |
| E5 | **Latency unusable** from multi-process runs; re-measure at concurrency 1 with the embedding isolated. | **OPEN** | — |
| E6 | **Per-preset / per-config checkpointing** — a crash used to lose the entire run. | **DONE** | `b08486e` |
| E7 | **Retries and fault isolation**: per-query search retry, gateway 5xx retry, per-query failure isolation in the agentic matrix. | **DONE** | `adc7de8`, `d12c23c` |
| E8 | **Harness fixes**: MCP schema validation before dispatch, allowlist-aware server instructions, anti-repeat prompt guidance. | **DONE** | `9973902` |
| E9 | **`any_gold_surfaced`** reported next to fractional recall (fair to an agent that correctly stops). | **DONE** | `472b897` |
| E10 | **Promote `any_gold_surfaced` to the headline agentic retrieval metric and demote `context_recall` to a diagnostic.** Evidence: `any_gold` correlates r=0.73 with task success, `context_recall` only r=0.48 — and `context_recall` correlates r=0.36 with *gold-set size*, a labelling artifact. Across gold sizes 1→5, success is flat (0.913→0.944) while `context_recall` falls 0.31 and `any_gold` *rises* to 0.986. Also add **answer-sufficiency** (does any retrieved document contain the verified answer?), which is gold-path-independent. | **OPEN** | — |
| E11 | **Run the statusboost × collapse 2×2.** The four-knob `hybrid_rrf` vs `full` difference is superadditive by +0.034: the same non-collapse knobs are worth +0.049 with collapse ON and +0.083 with it OFF (a boost-promoted wrong document costs 1 slot collapsed, up to 3 uncollapsed). Until this runs, do not quote "collapse is worth +0.083" — it is measured only in the presence of a boost that inflates it. | **OPEN** | — |

---

## Ship order (by expected quality gain, from the mechanism diagnostic)

| order | change | expected gain | confidence |
|---|---|---|---|
| 1 | **A0** query-shape routing for pasted values | **+0.11** overall (addresses 0.31 → 0.81) | high — bounded by a 0.743 per-query oracle |
| 2 | **A1** status boost → within-chain + tie-break ≤1.05 | **+0.057** [+0.047, +0.068] | very high |
| 3 | **A7** `fusion_rrf_k` 60 → 20 (ships with A6) | **+0.032**; ~+0.02 incremental after A1 | high |
| 4 | **A2b** gate identifier/semantic legs by query shape | +0.013 questions, +0.019 known-item; subsumed by A0 | medium |
| 5 | **A2** normalize the identifier field + filter value | 97.6% → ~0% failure on `identifier=` | very high (deterministic bug) |
| 6 | **E10** publish `any_gold_surfaced`, demote `context_recall` | reporting correctness, no model change | very high |
| 7 | **E11** statusboost × collapse 2×2; re-run on the fixed pool | prevents mis-attributing ±0.03 | — |
| 8 | **A2c** English (or per-language) analyzer for `text` | unmeasured; affects every config equally | medium |

A0+A1+A7 together rescue **45–51 of `full`'s 79 zero-scoring queries**, of which 73%
are ranking-policy failures (some existing config already finds them and `full`
buries them) rather than retrieval failures.

## Summary

**23 DONE**, **5 PARTIAL**, **24 OPEN**.

Everything blocking *trustworthy measurement* is done — the numbers in
`benchmark-results-2026-08-03.md` are produced by the fixed code. What remains OPEN
splits into two groups:

- **A1, A2, A4, A7, A8** — product work the benchmark has now justified with evidence.
  A1 is the single largest known quality win.
- **B1, B2, D3** — the benchmark's own credibility ceiling. Until graded gold and
  non-tautological known-item queries land, absolute numbers and cross-system claims
  (e.g. "we beat BM25") are not defensible; relative claims within the matrix are.
