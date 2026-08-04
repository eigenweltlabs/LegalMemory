"""Retrieval + agentic benchmark over a corpus packed from an open legal-task set — one system.

Additive to the appliance: the corpus is ingested through the existing ``local_fs``
connector and pipeline; gold is derived from the **source files** (never the
database), so every score exercises the whole insertion chain; evaluation reads
through the existing ``RetrievalService`` and the real MCP tool surface under
query-time config presets — no ingest-path or retrieval-logic changes.

Two tiers over one gold set and one index:

- **Agentic matrix** (``agentic_eval``) — the headline: agents consuming the RAG,
  compared across retrieval presets and tool allowlists.
- **Single-shot matrix** (``retrieval_eval``) — the diagnostic microscope and CI
  gate: rank metrics per preset (competitors + leave-one-out ablations), near-free.

See ``docs/benchmarking.md``.
"""

from knowledge_index.benchmark.agentic_eval import evaluate_agentic, render_agentic_markdown
from knowledge_index.benchmark.gold import generate_gold
from knowledge_index.benchmark.task_corpus import build_task_corpus
from knowledge_index.benchmark.presets import apply_preset, preset_names, resolve_presets
from knowledge_index.benchmark.retrieval_eval import (
    evaluate,
    evaluate_matrix,
    render_matrix_markdown,
    run_queries,
)
from knowledge_index.benchmark.store import freeze, list_frozen, resolve

__all__ = [
    "build_task_corpus",
    "generate_gold",
    "freeze",
    "resolve",
    "list_frozen",
    "evaluate",
    "evaluate_matrix",
    "evaluate_agentic",
    "render_matrix_markdown",
    "render_agentic_markdown",
    "run_queries",
    "apply_preset",
    "preset_names",
    "resolve_presets",
]
