"""Run the gold set through retrieval per baseline and produce a comparable report.

The scoring core (``run_queries``) takes an injectable ``search_fn`` so it is unit-
testable offline with canned hits; ``evaluate`` and ``evaluate_ladder`` wire that
core to a live ``RetrievalService`` the way the API's search endpoint
does — same ``(session_factory, config)`` shape. Baselines differ only by a
query-time config copy over one shared index, so the whole ladder is one pass over
the gold set per preset with no re-ingestion.

A run reports, per baseline: recall@k / MRR / nDCG@k (overall and by gold kind),
the rank-1 version-status mix (does collapse surface finals over drafts), and an
ethical-wall check (an outsider principal must get zero hits on every query). The
ladder adds the gate: the full system must beat ``naive_dense`` on nDCG@10 by a
configurable margin and leak nothing.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.benchmark import metrics
from knowledge_index.benchmark.baselines import LADDER, apply_baseline
from knowledge_index.benchmark.measure import percentiles
from knowledge_index.config import AppConfig
from knowledge_index.retrieval_types import SearchFilters

OUTSIDER_PRINCIPAL = "user:benchmark-outsider"


class _Hit(Protocol):
    source_paths: list[str]
    version_status: str


# (query, principals, filters) -> ranked hits
SearchFn = Callable[[str, set[str], SearchFilters | None], list[_Hit]]


def load_gold(gold_path: str | Path) -> list[dict]:
    """Load a frozen gold file (the committed benchmark artifact, decoupled from the
    regenerable corpus)."""
    return [
        json.loads(line)
        for line in Path(gold_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class CorpusCoverageError(RuntimeError):
    """The ingested corpus does not fully cover the gold — benchmarking would be a lie.

    Carries the coverage summary (including which gold documents are missing) so the
    failure is actionable.
    """

    def __init__(self, coverage: dict) -> None:
        self.coverage = coverage
        sample = ", ".join(coverage.get("missing", [])[:5])
        super().__init__(
            f"gold/corpus mismatch: {coverage['present']}/{coverage['gold_paths']} gold "
            f"documents present in the index (coverage {coverage['coverage']:.1%}). "
            f"Ingest the matching corpus (sync + run) before benchmarking. "
            f"Missing e.g.: {sample}"
        )


def _coverage_summary(gold_paths: set[str], present: set[str]) -> dict:
    """Pure coverage math: full iff every gold document is present in the index."""
    hit = gold_paths & present
    missing = sorted(gold_paths - present)
    return {
        "gold_paths": len(gold_paths),
        "present": len(hit),
        "coverage": round(len(hit) / len(gold_paths), 4) if gold_paths else 0.0,
        "full": bool(gold_paths) and not missing,
        "missing_count": len(missing),
        "missing": missing[:20],
    }


def corpus_coverage(session: Session, gold: list[dict]) -> dict:
    """Coverage of the gold's referenced documents in the ingested index.

    Guards the gold/corpus decoupling: anything short of full means the corpus that
    was synced is not the one this gold was frozen against, so scores are meaningless.
    """
    from sqlalchemy import select

    from knowledge_index.db.models import SourceObject

    gold_paths = {path for item in gold for path in item["gold_paths"]}
    present = (
        set(session.scalars(select(SourceObject.path).where(SourceObject.path.in_(gold_paths))))
        if gold_paths
        else set()
    )
    return _coverage_summary(gold_paths, present)


def run_queries(
    search_fn: SearchFn,
    gold: list[dict],
    *,
    ks: tuple[int, ...] = metrics.DEFAULT_KS,
    outsider_search_fn: SearchFn | None = None,
) -> dict:
    """Score every gold query; optionally probe each with an outsider principal.

    Pure aggregation over whatever ``search_fn`` returns — no database awareness.
    """
    scores: list[metrics.QueryScore] = []
    top1_status: Counter[str] = Counter()
    wall_leaks: list[str] = []
    latencies_ms: list[float] = []
    probed = 0

    for item in gold:
        principals = set(item["principals"])
        gold_paths = set(item["gold_paths"])
        started = time.monotonic()
        hits = search_fn(item["query"], principals, None)
        latencies_ms.append((time.monotonic() - started) * 1000)
        ranked_covers = [set(hit.source_paths) & gold_paths for hit in hits]
        scores.append(
            metrics.QueryScore.compute(item["id"], item["kind"], ranked_covers, gold_paths, ks)
        )
        if hits:
            top1_status[hits[0].version_status] += 1

        if outsider_search_fn is not None:
            probed += 1
            if outsider_search_fn(item["query"], {OUTSIDER_PRINCIPAL}, None):
                wall_leaks.append(item["id"])

    top1_total = sum(top1_status.values())
    non_draft = top1_total - top1_status.get("draft", 0)
    return {
        "metrics": metrics.aggregate(scores, ks),
        "latency_ms": percentiles(latencies_ms),
        "observations": {
            "top1_version_status": dict(top1_status),
            "final_not_draft_rate": round(non_draft / top1_total, 4) if top1_total else 0.0,
        },
        "ethical_wall": {
            "probed": probed,
            "leaks": wall_leaks,
            "clean": not wall_leaks,
        },
        "ks": list(ks),
    }


def _retrieval_search_fn(session: Session, config: AppConfig, *, limit: int) -> SearchFn:
    from knowledge_index.retrieval import RetrievalService

    service = RetrievalService(session, config)

    def search(query: str, principals: set[str], filters: SearchFilters | None) -> list[_Hit]:
        if not query.strip():
            return []
        return service.search_semantic(query, principals=principals, filters=filters, limit=limit)

    return search


def evaluate(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    gold_path: str | Path,
    *,
    baseline: str = "full",
    ks: tuple[int, ...] = metrics.DEFAULT_KS,
    check_ethical_wall: bool = True,
    require_full_coverage: bool = True,
) -> dict:
    """Evaluate one baseline against the live index behind ``session_factory``.

    ``gold_path`` is the frozen gold file; the matching corpus must already be synced
    and indexed. Unless ``require_full_coverage`` is disabled, this raises
    ``CorpusCoverageError`` *before* scoring when any gold document is absent from the
    index — a partial corpus yields meaningless numbers, so it fails rather than
    reporting a quietly deflated score.
    """
    gold = load_gold(gold_path)
    ablated = apply_baseline(config, baseline)
    limit = max(max(ks), 20)
    with session_factory() as session:
        coverage = corpus_coverage(session, gold)
        if require_full_coverage and not coverage["full"]:
            raise CorpusCoverageError(coverage)
        search_fn = _retrieval_search_fn(session, ablated, limit=limit)
        outsider_fn = search_fn if check_ethical_wall else None
        report = run_queries(search_fn, gold, ks=ks, outsider_search_fn=outsider_fn)
    report["baseline"] = baseline
    report["retrieval_config"] = ablated.retrieval.model_dump()
    report["gold_queries"] = len(gold)
    report["corpus"] = coverage
    return report


def evaluate_ladder(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    gold_path: str | Path,
    *,
    baselines: tuple[str, ...] = LADDER,
    ks: tuple[int, ...] = metrics.DEFAULT_KS,
    min_lift: float = 0.05,
    ndcg_k: int = 10,
) -> dict:
    """Evaluate the whole ladder over one index and apply the regression gate."""
    runs = {
        name: evaluate(session_factory, config, gold_path, baseline=name, ks=ks)
        for name in baselines
    }
    key = f"@{ndcg_k}"

    def _ndcg(name: str) -> float | None:
        if name not in runs:
            return None
        return runs[name]["metrics"]["overall"].get("ndcg", {}).get(key)

    full = _ndcg("full")
    naive = _ndcg("naive_dense")
    lift = round(full - naive, 4) if full is not None and naive is not None else None
    walls_clean = all(run["ethical_wall"]["clean"] for run in runs.values())
    corpus_full = all(run["corpus"]["full"] for run in runs.values())
    gate_passed = lift is not None and lift >= min_lift and walls_clean and corpus_full
    return {
        "gold_path": str(gold_path),
        "runs": runs,
        "comparison": {
            name: {
                "ndcg@10": run["metrics"]["overall"].get("ndcg", {}).get("@10"),
                "recall@10": run["metrics"]["overall"].get("recall", {}).get("@10"),
                "mrr": run["metrics"]["overall"].get("mrr"),
                "wall_clean": run["ethical_wall"]["clean"],
            }
            for name, run in runs.items()
        },
        "gate": {
            "metric": f"ndcg{key}",
            "full": full,
            "naive_dense": naive,
            "lift": lift,
            "min_lift": min_lift,
            "walls_clean": walls_clean,
            "corpus_full": corpus_full,
            "passed": gate_passed,
        },
    }
