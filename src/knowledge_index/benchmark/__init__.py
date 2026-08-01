"""Retrieval-quality benchmark over a corpus packed from an open legal-task set.

Additive to the appliance: the corpus is ingested through the existing ``local_fs``
connector and pipeline, and evaluation reads through the existing ``RetrievalService``
under query-time config ablations — no ingest-path or retrieval-logic changes. See
``docs/benchmarking.md``.
"""

from knowledge_index.benchmark.baselines import LADDER, apply_baseline, baseline_names
from knowledge_index.benchmark.gold import derive_gold, write_gold
from knowledge_index.benchmark.gold_llm import generate_llm_gold
from knowledge_index.benchmark.harness import evaluate, evaluate_ladder, run_queries
from knowledge_index.benchmark.task_corpus import build_task_corpus
from knowledge_index.benchmark.store import freeze, list_frozen, resolve

__all__ = [
    "build_task_corpus",
    "derive_gold",
    "write_gold",
    "generate_llm_gold",
    "freeze",
    "resolve",
    "list_frozen",
    "evaluate",
    "evaluate_ladder",
    "run_queries",
    "apply_baseline",
    "baseline_names",
    "LADDER",
]
