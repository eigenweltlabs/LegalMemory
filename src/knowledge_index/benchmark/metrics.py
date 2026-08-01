"""Retrieval-quality metrics — pure functions, no database or model access.

Relevance is binary: a ranked hit is relevant if it *covers* a gold document
(one of the hit's source paths equals a gold path). A single hit can cover more
than one gold document (version dedup folds several source paths behind one
version), so every function is written against ``ranked_covers`` — the ordered
list of gold-id sets each ranked position newly brings in — rather than a flat
list of ids. Everything here is deterministic and unit-testable offline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def recall_at_k(ranked_covers: list[set[str]], gold: set[str], k: int) -> float:
    """Fraction of gold documents whose covering hit appears within the top ``k``."""
    if not gold:
        return 0.0
    seen: set[str] = set()
    for covers in ranked_covers[:k]:
        seen |= covers & gold
    return len(seen) / len(gold)


def precision_at_k(ranked_covers: list[set[str]], gold: set[str], k: int) -> float:
    """Fraction of the top ``k`` positions that cover at least one *new* gold doc."""
    if k <= 0:
        return 0.0
    seen: set[str] = set()
    relevant_positions = 0
    for covers in ranked_covers[:k]:
        fresh = (covers & gold) - seen
        if fresh:
            relevant_positions += 1
            seen |= fresh
    return relevant_positions / k


def reciprocal_rank(ranked_covers: list[set[str]], gold: set[str]) -> float:
    """Reciprocal of the rank of the first hit covering any gold document."""
    for index, covers in enumerate(ranked_covers, start=1):
        if covers & gold:
            return 1.0 / index
    return 0.0


def ndcg_at_k(ranked_covers: list[set[str]], gold: set[str], k: int) -> float:
    """Binary-relevance nDCG@k; each gold document scores once, at its first hit."""
    if not gold:
        return 0.0
    dcg = 0.0
    seen: set[str] = set()
    for index, covers in enumerate(ranked_covers[:k], start=1):
        fresh = (covers & gold) - seen
        if fresh:
            dcg += 1.0 / math.log2(index + 1)
            seen |= fresh
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, min(k, len(gold)) + 1))
    return dcg / ideal if ideal else 0.0


DEFAULT_KS: tuple[int, ...] = (1, 5, 10, 20)


@dataclass
class QueryScore:
    """Per-query metric bundle plus the raw material to aggregate a run."""

    query_id: str
    kind: str
    gold_size: int
    recall: dict[int, float]
    precision: dict[int, float]
    mrr: float
    ndcg: dict[int, float]

    @classmethod
    def compute(
        cls,
        query_id: str,
        kind: str,
        ranked_covers: list[set[str]],
        gold: set[str],
        ks: tuple[int, ...] = DEFAULT_KS,
    ) -> "QueryScore":
        return cls(
            query_id=query_id,
            kind=kind,
            gold_size=len(gold),
            recall={k: recall_at_k(ranked_covers, gold, k) for k in ks},
            precision={k: precision_at_k(ranked_covers, gold, k) for k in ks},
            mrr=reciprocal_rank(ranked_covers, gold),
            ndcg={k: ndcg_at_k(ranked_covers, gold, k) for k in ks},
        )


def aggregate(scores: list[QueryScore], ks: tuple[int, ...] = DEFAULT_KS) -> dict:
    """Mean each metric across queries, overall and per gold kind."""

    def _mean_block(subset: list[QueryScore]) -> dict:
        if not subset:
            return {"queries": 0}
        n = len(subset)
        return {
            "queries": n,
            "recall": {f"@{k}": round(sum(s.recall[k] for s in subset) / n, 4) for k in ks},
            "precision": {f"@{k}": round(sum(s.precision[k] for s in subset) / n, 4) for k in ks},
            "mrr": round(sum(s.mrr for s in subset) / n, 4),
            "ndcg": {f"@{k}": round(sum(s.ndcg[k] for s in subset) / n, 4) for k in ks},
        }

    kinds = sorted({score.kind for score in scores})
    return {
        "overall": _mean_block(scores),
        "by_kind": {kind: _mean_block([s for s in scores if s.kind == kind]) for kind in kinds},
    }
