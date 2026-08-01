---
title: Benchmarks
description: Measuring retrieval quality against a naive-RAG baseline with frozen, deterministic gold labels — plus the end-to-end task benchmark.
---

How we measure *retrieval quality* — does the shadow index surface the right
documents for a query. It is additive: the corpus is ingested through the
existing `local_fs` connector and pipeline, and evaluation reads through the
existing `RetrievalService`. Nothing in the ingest path or retrieval logic
changes; a baseline is only a query-time config copy.

## The data — an open legal-task set shaped as a firm

The upstream dataset ([source repository](https://github.com/harveyai/harvey-labs), MIT) ships ~1,671 legal tasks,
each a `task.json` (instructions + PASS/FAIL criteria) plus a `documents/` bundle of
real `.docx/.xlsx/.eml`. `generate-benchmark` packs those bundles into a German-firm
`mock_dms/` tree — one **matter per instrument**, its task-type folders (first-draft,
first-turn-redline, …) supplying version-chain material, one **ACL group per practice
area** so cross-area queries exercise the ethical walls, and every other matter's
documents acting as retrieval distractors. Output:
`mock_dms/`, `acl-by-path.json`, `scenarios.jsonl`, `scenario.json`, plus
`retrieval-gold.jsonl`.

> **Jurisdiction caveat.** The task set is US-law / English. It stresses retrieval
> mechanics, scale, and version sprawl, but not German OCR, umlaut paths, or German
> identifiers (Aktenzeichen, HRB, §-refs).

## The gold labels (derived, deterministic)

The dataset labels *output*, not retrieval, so `retrieval-gold.jsonl` is derived from the
packed corpus with no model:

- **`instruction_working_set`** — query = the task instruction, gold = the scenario's
  whole bundle. "Can retrieval assemble this matter's working set from the ask?"
- **`factoid`** — a discriminative value quoted in a PASS/FAIL criterion (party name,
  account/charter number) that appears in some-but-not-all bundle docs, turned into a
  pasted-value query with gold = the documents that contain it. Exercises the lexical
  and identifier legs. Best-effort and data-dependent; an optional LLM/human pass can
  densify these without changing the file contract.

## Baselines — one index, query-time ablation

Every distinguishing knob (`weight_*`, `fusion_rrf_k`, `version_status_boost`,
`collapse_per_document`, `rerank_enabled`) is read by `RetrievalService` at query
time, so the whole ladder runs over **one index** by handing retrieval a different
`AppConfig` copy — no re-ingestion:

| Baseline | What it is |
|---|---|
| `bm25` | lexical leg only — the near-free floor |
| `naive_dense` | single embedding leg, flat top-k, no fusion / collapse / rerank / status boost — **the simple baseline** |
| `full` | shipped defaults (all legs, identifier weight, collapse, rerank) — the target |

## Metrics & gate

recall@{1,5,10,20}, MRR@10, nDCG@10 (overall and per gold kind), the rank-1
version-status mix (does collapse surface finals over drafts), and an ethical-wall
check (an outsider principal must get zero hits on every query). The **gate**:
`full` must beat `naive_dense` on nDCG@10 by `--min-lift`, every baseline must leak
nothing, and the corpus must fully cover the gold (incomplete coverage fails the run
before scoring).

## Gold is frozen once, then read every run

Creating gold (deterministic + optional LLM + human review) is a **one-time** job.
The result is committed under `src/knowledge_index/benchmark/data/` as a `*.gold.jsonl`
+ `*.meta.json` pair and read by every evaluation; the bulky document corpus stays
regenerable and git-ignored. `run-retrieval-eval` never regenerates gold — it takes a
frozen set *name* (or a path) and scores it against the live index. It **fails fast**
if the ingested corpus does not fully cover the gold (every referenced document must
be present), listing the missing documents, rather than reporting a silently deflated
score.

### One-time: create and freeze gold

```bash
# clone the task dataset (sparse keeps it small)
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/harveyai/harvey-labs external/task-set
( cd external/task-set && git sparse-checkout set tasks/contracts )

# pack the corpus + derive deterministic gold (pure files, no stack)
ki generate-benchmark testdata/benchmark --source external/task-set \
  --areas contracts/banking --matters 50 --docs-target 1000

# optional: enrich gold with LLM-generated, source-verified labels (needs the gateway)
ki derive-llm-gold testdata/benchmark --per-scenario 4

# review testdata/benchmark/retrieval-gold.jsonl, then commit it into the package
ki freeze-gold testdata/benchmark --name contracts-banking
```

### Every run: regenerate corpus, ingest, score the frozen gold

```bash
# regenerate the same corpus (deterministic — same paths the gold references)
ki generate-benchmark testdata/benchmark --source external/task-set \
  --areas contracts/banking --matters 50 --docs-target 1000

# ingest through the normal pipeline (real stack)
docker compose exec app ki add-source /testdata/benchmark/mock_dms \
  --name "Retrieval-Bench" --acl-map /testdata/benchmark/acl-by-path.json
docker compose exec app ki sync && docker compose exec app ki run --limit 100000

# score the ladder against the committed gold and apply the gate
ki run-retrieval-eval contracts-banking --baseline ladder \
  --report retrieval-report.json --min-lift 0.05
# single baseline: --baseline naive_dense   |   ad-hoc file: run-retrieval-eval path/to.gold.jsonl
```

The report JSON carries per-baseline metrics, `corpus` coverage, plus the exact
`retrieval` config, so runs are comparable over time and diffable in review — rerun
before and after each retrieval change so quality is regression-tested, not vibes.

## Next: end-to-end rubric scoring (phase 2)

The same corpus feeds a task-success benchmark: an agent gets a task instruction,
acquires context *only* through the MCP tools over the full firm, produces work
product, and is scored by an LLM judge against the untouched `task.json` criteria —
`EvalRecord` already models exactly this shape. Baseline for that layer = the same
agent wired to the `naive_dense` retriever.
