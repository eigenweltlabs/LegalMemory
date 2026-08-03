# Frozen retrieval gold

Committed, version-controlled benchmark gold. Each set is two files:

- `<name>.gold.jsonl` — the gold queries (query → relevant documents)
- `<name>.meta.json` — the corpus config it was frozen against (source, areas, seed,
  `content_hash`, counts), so the exact corpus is reproducible and can be matched
  before scoring.

Gold is created **once** — `ki generate-gold` (LLM proposals, machine-verified:
verbatim answer check + corpus-wide discrimination; no human review step) — then
frozen here with `ki freeze-gold`. `run-retrieval-eval` and `run-agentic-eval` read
these files by name; they never regenerate them. The document corpus itself is
regenerable and git-ignored; only this small gold is tracked.

Two gold kinds: `question` (natural lawyer question with a verified in-document
answer) and `known_item` (a pasted identifier/value lookup). See
`docs/benchmarking.md`.

Queries and referenced values are derived from **[harveyai/harvey-labs](https://github.com/harveyai/harvey-labs)**
(MIT). Regenerate any set with the `reproduce` command in its `.meta.json`.

`firm-structure-*.json` files are corpus-build structure manifests (used by
`generate-benchmark --structure`), not gold.
