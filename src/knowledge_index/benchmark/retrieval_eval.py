"""Single-shot retrieval matrix: run the gold through every preset, compare with CIs.

The scoring core (``run_queries``) takes an injectable ``search_fn`` so it is unit-
testable offline with canned hits; ``evaluate`` and ``evaluate_matrix`` wire that
core to a live ``RetrievalService``. Presets differ only by a query-time config copy
over one shared index (see ``presets.py``), so the whole matrix is one pass over the
gold per preset with no re-ingestion.

Per preset the report carries recall@k / MRR / nDCG@k (overall and by gold kind),
latency percentiles, the rank-1 version-status mix, an ethical-wall probe (an
outsider principal must get zero hits on every query), and a paired-bootstrap 95% CI
on per-query nDCG@10 against ``full`` — every row reads as "delta vs full, with
error bars". The gate: ``full`` must beat ``naive_dense`` by ``min_lift``, every
preset must leak nothing, and the corpus must fully cover the gold (incomplete
coverage fails the run before scoring — a partial corpus yields meaningless numbers).

This tier is the diagnostic microscope and CI gate; the headline benchmark is the
agentic tier (``agentic_eval.py``), which consumes retrieval the way agents do.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Protocol

from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.benchmark import metrics
from knowledge_index.benchmark.measure import percentiles
from knowledge_index.benchmark.presets import apply_preset
from knowledge_index.config import AppConfig
from knowledge_index.retrieval_types import SearchFilters

OUTSIDER_PRINCIPAL = "user:benchmark-outsider"
_WS = re.compile(r"\s+")


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
    concurrency: int = 1,
) -> dict:
    """Score every gold query; optionally probe each with an outsider principal.

    Pure aggregation over whatever ``search_fn`` returns — no database awareness.
    ``per_query`` (id → nDCG@10) is kept in the report so matrix comparisons can
    bootstrap paired deltas.

    ``answer_in_context@k`` is the passage-level auxiliary: for gold that carries a
    verified answer, was the answer string present in the *text* of the top-k hits?
    Document-level metrics stay primary (a hit's excerpt is a snippet, so this reads
    conservatively) — the column exists to expose configs that find the right
    document via the wrong content.
    """
    def _score_one(item: dict) -> dict:
        principals = set(item["principals"])
        gold_paths = set(item["gold_paths"])
        primary, gains = metrics.graded_gold(item)
        started = time.monotonic()
        hits = search_fn(item["query"], principals, None)
        latency = (time.monotonic() - started) * 1000
        ranked_covers = [set(hit.source_paths) & gold_paths for hit in hits]
        answer = _WS.sub(" ", (item.get("meta") or {}).get("answer", "")).strip().casefold()
        # NOTE: `excerpt` is a ~320-char window anchored on a query term, not the
        # chunk text — so this measures whether the snippet happened to frame the
        # answer, and a config returning several chunks per document gets several
        # windows per document. Diagnostic only; deliberately not a headline column.
        seen_documents: set[str] = set()
        answer_at = {k: False for k in ks}
        if answer:
            for rank, hit in enumerate(hits, start=1):
                key = getattr(hit, "document_id", None) or ",".join(hit.source_paths)
                if key in seen_documents:  # one window per document, not per chunk
                    continue
                seen_documents.add(key)
                if answer in _WS.sub(" ", getattr(hit, "excerpt", "") or "").casefold():
                    for k in ks:
                        if rank <= k:
                            answer_at[k] = True
        leaked = outsider_search_fn is not None and bool(
            outsider_search_fn(item["query"], {OUTSIDER_PRINCIPAL}, None)
        )
        return {
            "score": metrics.QueryScore.compute(
                item["id"],
                (item.get("meta") or {}).get("anchor", "none"),
                ranked_covers,
                gold_paths,
                ks,
                primary=primary,
                gains=gains,
            ),
            "latency": latency,
            "top1": hits[0].version_status if hits else None,
            "has_answer": bool(answer),
            "answer_at": answer_at,
            "leaked": leaked,
            "id": item["id"],
        }

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(_score_one, gold))
    else:
        results = [_score_one(item) for item in gold]

    scores = [r["score"] for r in results]
    latencies_ms = [r["latency"] for r in results]
    top1_status: Counter[str] = Counter(r["top1"] for r in results if r["top1"])
    wall_leaks = [r["id"] for r in results if r["leaked"]]
    probed = len(results) if outsider_search_fn is not None else 0
    answered_queries = sum(1 for r in results if r["has_answer"])
    answer_hits = {k: sum(1 for r in results if r["answer_at"][k]) for k in ks}

    top1_total = sum(top1_status.values())
    non_draft = top1_total - top1_status.get("draft", 0)
    return {
        "metrics": metrics.aggregate(scores, ks),
        "answer_in_context": {
            "queries_with_answer": answered_queries,
            **{
                f"@{k}": round(count / answered_queries, 4) if answered_queries else None
                for k, count in answer_hits.items()
            },
        },
        "per_query": {score.query_id: score.ndcg.get(10, 0.0) for score in scores},
        "latency_ms": percentiles(latencies_ms),
        "observations": {
            "top1_version_status": dict(top1_status),
            "final_not_draft_rate": round(non_draft / top1_total, 4) if top1_total else 0.0,
        },
        "ethical_wall": {
            "probed": probed,
            "leaks": wall_leaks,
            # None, never True, when nothing was probed: a security assertion must
            # not read as satisfied because the check did not run.
            "clean": (not wall_leaks) if probed else None,
        },
        "ks": list(ks),
    }


def _retrieval_search_fn(
    session: Session, config: AppConfig, *, limit: int, stats: dict | None = None
) -> SearchFn:
    from knowledge_index.retrieval import RetrievalService

    service = RetrievalService(session, config)
    fallback_service: RetrievalService | None = None

    def search(query: str, principals: set[str], filters: SearchFilters | None) -> list[_Hit]:
        nonlocal fallback_service
        if not query.strip():
            return []
        # The product's search deliberately raises on model faults (e.g. a rerank
        # response that fails schema validation) with no silent fallback. Correct for
        # production visibility — but across ~1e3 benchmark queries one transient
        # model hiccup would kill the whole preset run, so the benchmark retries the
        # query a few times before degrading.
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return service.search_semantic(
                    query, principals=principals, filters=filters, limit=limit
                )
            except Exception as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
        # Persistent rerank failure (some prompts deterministically break the rerank
        # model's structured output): score THIS query on the un-reranked fused
        # ranking instead of killing the preset, and count it honestly in the report.
        if config.retrieval.rerank_enabled:
            if fallback_service is None:
                degraded = config.model_copy(deep=True)
                degraded.retrieval.rerank_enabled = False
                fallback_service = RetrievalService(session, degraded)
            if stats is not None:
                stats["rerank_fallbacks"] = stats.get("rerank_fallbacks", 0) + 1
            return fallback_service.search_semantic(
                query, principals=principals, filters=filters, limit=limit
            )
        raise last_error

    return search


def evaluate(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    gold_path: str | Path,
    *,
    preset: str = "full",
    ks: tuple[int, ...] = metrics.DEFAULT_KS,
    check_ethical_wall: bool = False,
    require_full_coverage: bool = True,
    concurrency: int = 1,
) -> dict:
    """Evaluate one preset against the live index behind ``session_factory``.

    ``gold_path`` is the frozen gold file; the matching corpus must already be synced
    and indexed. Unless ``require_full_coverage`` is disabled, this raises
    ``CorpusCoverageError`` *before* scoring when any gold document is absent from
    the index.

    ``concurrency`` > 1 fans queries over a thread pool; each worker thread gets its
    own session + ``RetrievalService`` (sessions are not thread-safe). Latency
    percentiles then measure a loaded system rather than idle single-query latency —
    the report carries ``concurrency`` so the caveat is visible.
    """
    gold = load_gold(gold_path)
    ablated = apply_preset(config, preset)
    limit = max(max(ks), 20)
    sessions: list[Session] = []
    sessions_lock = threading.Lock()
    local = threading.local()
    search_stats: dict = {}

    def thread_search(query: str, principals: set[str], filters: SearchFilters | None):
        if not hasattr(local, "fn"):
            session = session_factory()
            with sessions_lock:
                sessions.append(session)
            local.fn = _retrieval_search_fn(session, ablated, limit=limit, stats=search_stats)
        return local.fn(query, principals, filters)

    try:
        with session_factory() as session:
            coverage = corpus_coverage(session, gold)
            if require_full_coverage and not coverage["full"]:
                raise CorpusCoverageError(coverage)
        outsider_fn = thread_search if check_ethical_wall else None
        report = run_queries(
            thread_search, gold, ks=ks, outsider_search_fn=outsider_fn, concurrency=concurrency
        )
    finally:
        for session in sessions:
            session.close()
    report["preset"] = preset
    report["retrieval_config"] = ablated.retrieval.model_dump()
    report["gold_queries"] = len(gold)
    report["corpus"] = coverage
    report["concurrency"] = concurrency
    report["rerank_fallbacks"] = search_stats.get("rerank_fallbacks", 0)
    return report


def _paired_ndcg(runs: dict[str, dict], treatment: str, reference: str) -> dict:
    """Bootstrap the per-query nDCG@10 delta between two runs, paired on query id."""
    ids = sorted(runs[reference]["per_query"])
    return metrics.paired_bootstrap(
        [runs[treatment]["per_query"].get(query_id, 0.0) for query_id in ids],
        [runs[reference]["per_query"][query_id] for query_id in ids],
    )


def evaluate_matrix(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    gold_path: str | Path,
    *,
    presets: tuple[str, ...],
    ks: tuple[int, ...] = metrics.DEFAULT_KS,
    min_lift: float = 0.05,
    concurrency: int = 1,
    checkpoint_dir: str | Path | None = None,
    check_ethical_wall: bool = False,
) -> dict:
    """Evaluate a preset matrix over one index; compare against ``full`` with CIs.

    ``presets`` always contains ``full`` (``resolve_presets`` guarantees it). The
    gate anchors on ``naive_dense`` when it is in the matrix: ``full`` must beat it
    on mean nDCG@10 by ``min_lift``, the lift must be bootstrap-significant, every
    preset's wall must be clean, and coverage must be full.

    ``checkpoint_dir`` makes the matrix crash-safe: each preset's run is written to
    ``<dir>/<preset>.json`` the moment it completes, and presets with a valid
    checkpoint (same gold fingerprint) are loaded instead of re-run — so a crash or
    kill costs at most the preset in flight, and a re-invocation resumes.
    """
    fingerprint = hashlib.sha256(Path(gold_path).read_bytes()).hexdigest()[:16]
    ckpt = Path(checkpoint_dir) if checkpoint_dir else None
    if ckpt:
        ckpt.mkdir(parents=True, exist_ok=True)

    runs: dict[str, dict] = {}
    for name in presets:
        ckpt_file = ckpt / f"{name}.json" if ckpt else None
        if ckpt_file and ckpt_file.is_file():
            cached = json.loads(ckpt_file.read_text(encoding="utf-8"))
            if cached.get("gold_fingerprint") == fingerprint:
                runs[name] = cached
                continue  # stale checkpoints (different gold) are simply re-run
        run = evaluate(
            session_factory,
            config,
            gold_path,
            preset=name,
            ks=ks,
            concurrency=concurrency,
            check_ethical_wall=check_ethical_wall,
        )
        run["gold_fingerprint"] = fingerprint
        runs[name] = run
        if ckpt_file:
            ckpt_file.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")

    comparison = {}
    for name, run in runs.items():
        overall = run["metrics"]["overall"]
        comparison[name] = {
            "ndcg@10": overall.get("ndcg", {}).get("@10"),
            "recall@10": overall.get("recall", {}).get("@10"),
            "mrr": overall.get("mrr"),
            "answer_in_context@10": run["answer_in_context"].get("@10"),
            "p95_ms": run["latency_ms"].get("p95"),
            # wall status is meaningful only when probing was on (a quality-focused
            # run reports None here rather than a vacuous "clean")
            "wall_clean": (
                run["ethical_wall"]["clean"] if run["ethical_wall"]["probed"] else None
            ),
            "vs_full": None if name == "full" else _paired_ndcg(runs, name, "full"),
        }

    probed_runs = [run for run in runs.values() if run["ethical_wall"]["probed"]]
    walls_clean = (
        all(run["ethical_wall"]["clean"] is True for run in probed_runs) if probed_runs else None
    )
    corpus_full = all(run["corpus"]["full"] for run in runs.values())
    gate: dict = {
        "metric": "ndcg@10",
        "reference": "naive_dense",
        "min_lift": min_lift,
        "walls_clean": walls_clean,
        "corpus_full": corpus_full,
    }
    # the quality gate: lift over naive_dense + full coverage; a wall LEAK fails it
    # too when probing was enabled, but walls are otherwise out of scope
    walls_ok = walls_clean is not False
    if "naive_dense" in runs:
        bootstrap = _paired_ndcg(runs, "full", "naive_dense")
        lift = bootstrap.get("delta")
        gate.update(
            {
                "lift": lift,
                "ci95": bootstrap.get("ci95"),
                "significant": bootstrap.get("significant", False),
                "passed": (
                    lift is not None
                    and lift >= min_lift
                    and bool(bootstrap.get("significant"))
                    and walls_ok
                    and corpus_full
                ),
            }
        )
    else:
        gate.update({"lift": None, "passed": walls_ok and corpus_full})

    return {
        "gold_path": str(gold_path),
        "presets": list(presets),
        "runs": runs,
        "comparison": comparison,
        "gate": gate,
    }


def render_matrix_markdown(report: dict) -> str:
    """The comparison as a markdown table — the piece of the report humans read."""
    # Sliced by anchor — whether the request cites an identifier/entity/amount —
    # which is the axis the lexical and identifier legs are supposed to serve.
    lines = [
        "| preset | nDCG@10 | anchored | unanchored | Δ vs full (95% CI) "
        "| recall@10 | MRR | wall |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name in report["presets"]:
        row = report["comparison"][name]
        versus = row["vs_full"]
        if versus is None:
            delta = "—"
        else:
            low, high = versus.get("ci95", [None, None])
            mark = " *" if versus.get("significant") else ""
            delta = f"{versus['delta']:+.4f} [{low:+.4f}, {high:+.4f}]{mark}"
        by_kind = report["runs"][name]["metrics"]["by_kind"]
        anchored = [v for k, v in by_kind.items() if k != "none"]
        anchored_ndcg = (
            round(sum(b["ndcg"]["@10"] * b["queries"] for b in anchored)
                  / sum(b["queries"] for b in anchored), 4)
            if anchored else "—"
        )
        unanchored = by_kind.get("none", {}).get("ndcg", {}).get("@10", "—")
        lines.append(
            f"| {name} | {row['ndcg@10']} | {anchored_ndcg} | {unanchored} "
            f"| {delta} | {row['recall@10']} "
            f"| {row['mrr']} "
            f"| {'—' if row['wall_clean'] is None else 'clean' if row['wall_clean'] else 'LEAK'} |"
        )
    gate = report["gate"]
    lines.append("")
    lines.append(
        f"Gate (full vs {gate['reference']}, min lift {gate['min_lift']}): "
        f"lift={gate.get('lift')} ci95={gate.get('ci95')} "
        f"significant={gate.get('significant')} walls_clean={gate['walls_clean']} "
        f"corpus_full={gate['corpus_full']} → "
        f"{'PASSED' if gate.get('passed') else 'FAILED'}"
    )
    lines.append("")
    lines.append("`*` = bootstrap-significant at 95% (CI excludes zero)")
    lines.append(
        "Diagnostic tier: gold items are user requests, so these numbers say how much "
        "work the agent has to do to recover — not how good search is for a human. "
        "`anchored` = the request cites an identifier / entity / amount."
    )
    return "\n".join(lines)
