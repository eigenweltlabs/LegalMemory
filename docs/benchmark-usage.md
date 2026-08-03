# Benchmark — usage guide

How to run the benchmark end-to-end. For the design rationale (why source-derived
gold, the two tiers, the preset philosophy) see `docs/benchmarking.md`.

## Prerequisites

1. **The stack up** with a reachable gateway:
   ```bash
   docker compose up -d --build
   bash scripts/bootstrap-hatchet.sh
   ```
2. **A Harvey LAB checkout** (MIT) for corpus generation — sparse is fine:
   ```bash
   git clone --depth 1 --filter=blob:none --sparse \
     https://github.com/harveyai/harvey-labs external/harvey-labs
   ( cd external/harvey-labs && git sparse-checkout set tasks )
   ```

## 1. Generate a corpus

```bash
ki generate-benchmark testdata/harvey-full --source external/harvey-labs \
  --matters 2000 --docs-target 20000 --seed 42
```

Packs Harvey task bundles into a DMS tree (`mock_dms/`), plus `acl-by-path.json` and
`scenarios.jsonl`. `--layout firm --structure …` and `--noise …` build the realistic
firm tree instead (see `--help`). Corpus only — gold is a separate step.

## 2. Generate gold (LLM + machine verification, no review step)

```bash
ki generate-gold testdata/harvey-full \
  --limit-scenarios 300 --per-scenario 4 --model-slot judge --max-gold-docs 5
```

One LLM pass per scenario (stratified across practice areas) proposing `question` and
`known_item` labels; each proposal must survive verbatim answer verification and the
corpus-wide discrimination check. Rejections land in `rejected.jsonl` with reasons.
Re-running is safe (dedupe) — raise `--limit-scenarios` to grow the set.

## 3. Freeze the gold

```bash
ki freeze-gold testdata/harvey-full --name harvey-full
```

Copies the gold into `src/knowledge_index/benchmark/data/harvey-full.gold.jsonl`
(+ meta). Commit it. Evals read frozen sets by name.

## 4. Ingest the corpus

```bash
docker compose exec app ki bench-ingest /testdata/harvey-full --name harvey-full
```

add-source → sync → pipeline, reporting docs/hour and per-stage cost. (Skip if the
corpus is already ingested — evals check coverage and refuse to score a partial
corpus.)

## 5. Tier 2 — single-shot retrieval matrix (fast, near-free)

```bash
# the standard comparison ladder + gate (CI)
ki run-retrieval-eval harvey-full --presets competitors --min-lift 0.05 --report competitors.json

# the feature ablation
ki run-retrieval-eval harvey-full --presets ablations --report ablations.json

# everything, or a hand-picked list
ki run-retrieval-eval harvey-full --presets all
ki run-retrieval-eval harvey-full --presets full,hybrid_rrf_rerank,full_no_identifier
```

Prints a markdown table (nDCG@10, Δ vs `full` with bootstrap CI, recall, MRR, p95
latency, wall status); `--report` writes the full JSON including per-preset
retrieval config, so runs are diffable in review. Exit code 1 when the gate fails.

## 6. Tier 1 — agentic matrix (the headline; costs model spend)

```bash
ki run-agentic-eval harvey-full --configs default --limit 200 \
  --agent-slot extract --judge-slot judge --report agentic.json
```

Runs each sampled query through the config matrix (`classic_rag`, `agent_naive`,
`agent_hybrid`, `agent_full_search`, `agent_full_filters`, `agent_full_tools`;
`--configs all` adds `agent_hybrid_rerank`). Prints success rate, answer accuracy
(questions), known-item success, context recall, Δ vs `agent_full_tools` with CI,
tool calls, and $/query — plus the outsider-agent wall probe. The JSON report
carries every trajectory for debugging.

Cost control: `--limit` samples stratified across gold kinds; `--max-steps` caps the
tool loop; spend is read from LiteLLM, never estimated.

## Reading the numbers

- `*` in a Δ column = bootstrap-significant at 95% (CI excludes zero). An
  insignificant delta is noise — don't ship a conclusion on it.
- Per-kind breakdown is the diagnosis: `known_item` reacts to the lexical/identifier
  legs; `question` to the semantic side (profiles, contextualization).
- **Scale matters**: metrics only discriminate on a multi-matter corpus with
  distractors and ACL walls.
- Coverage failure means the frozen gold and the ingested corpus have diverged —
  regenerate/ingest the matching corpus; the eval will not score a partial one.
