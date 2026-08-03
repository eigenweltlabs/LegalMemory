# Frozen retrieval gold

Committed, version-controlled benchmark gold. Each set is two files:

- `<name>.gold.jsonl` — the gold queries (query → relevant documents)
- `<name>.meta.json` — the corpus config it was frozen against (source, areas, seed,
  `content_hash`, counts), so the exact corpus is reproducible and can be matched
  before scoring.

Gold is created **once** — deterministic derivation (`generate-benchmark`), optional
LLM enrichment (`derive-llm-gold`), human review — then frozen here with
`ki freeze-gold`. `run-retrieval-eval <name>` reads these files; it never regenerates
them. The document corpus itself is regenerable and git-ignored; only this small,
curated gold is tracked.

Queries and referenced values are derived from **[harveyai/harvey-labs](https://github.com/harveyai/harvey-labs)**
(MIT). Regenerate any set with the `reproduce` command in its `.meta.json`.
