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
import random
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


PRIMARY_GAIN = 1.0
SECONDARY_GAIN = 0.3


def graded_gold(item: dict) -> tuple[set[str], dict[str, float]]:
    """Split one gold item into ``(primary paths, path -> gain)``.

    A request is *answered* by exactly one document — the primary. The other gold
    paths only co-mention the anchor, and grading them equally is what made gold
    read as ~2.5 documents per request: a config that returned the one document
    answering the question scored recall 0.4, not 1.0. So recall and MRR are taken
    against the primary alone, while nDCG keeps the secondaries at a reduced gain
    so surfacing the corroborating documents is still worth something.

    This is the only place that knows the gold schema's grading fields; both eval
    tiers go through it so they cannot drift apart.
    """
    paths = list(item.get("gold_paths") or [])
    primary_path = (item.get("meta") or {}).get("primary_path")
    primary = {primary_path} & set(paths) if primary_path else set()
    if not primary:  # gold written before grading existed — degrade to flat
        primary = set(paths)
    gains = {path: (PRIMARY_GAIN if path in primary else SECONDARY_GAIN) for path in paths}
    return primary, gains


def ndcg_at_k_graded(ranked_covers: list[set[str]], gains: dict[str, float], k: int) -> float:
    """Graded nDCG@k over per-document gains.

    A ranked position that newly brings in several gold documents is credited once,
    with the best gain among them — the same one-credit-per-position convention the
    binary version uses, so version-collapsed hits are treated identically by both.
    """
    if not gains:
        return 0.0
    dcg = 0.0
    seen: set[str] = set()
    for index, covers in enumerate(ranked_covers[:k], start=1):
        fresh = covers - seen
        if fresh:
            dcg += max(gains.get(doc, 0.0) for doc in fresh) / math.log2(index + 1)
            seen |= fresh
    ideal = sum(
        gain / math.log2(index + 1)
        for index, gain in enumerate(sorted(gains.values(), reverse=True)[:k], start=1)
    )
    return dcg / ideal if ideal else 0.0


DEFAULT_KS: tuple[int, ...] = (1, 5, 10, 20)


@dataclass
class QueryScore:
    """Per-query metric bundle plus the raw material to aggregate a run."""

    query_id: str
    kind: str
    gold_size: int
    primary_size: int
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
        *,
        primary: set[str] | None = None,
        gains: dict[str, float] | None = None,
    ) -> "QueryScore":
        """Score one query. Pass ``primary``/``gains`` from :func:`graded_gold`;
        omitting them falls back to flat binary relevance over ``gold``."""
        primary = primary if primary else set(gold)
        gains = gains if gains else dict.fromkeys(gold, PRIMARY_GAIN)
        return cls(
            query_id=query_id,
            kind=kind,
            gold_size=len(gold),
            primary_size=len(primary),
            # Recall and MRR ask "did it find the document that answers this?" —
            # the secondaries are corroboration, not the target.
            recall={k: recall_at_k(ranked_covers, primary, k) for k in ks},
            precision={k: precision_at_k(ranked_covers, gold, k) for k in ks},
            mrr=reciprocal_rank(ranked_covers, primary),
            ndcg={k: ndcg_at_k_graded(ranked_covers, gains, k) for k in ks},
        )


def aggregate(scores: list[QueryScore], ks: tuple[int, ...] = DEFAULT_KS) -> dict:
    """Mean each metric across queries, overall and per gold kind."""

    def _mean_block(subset: list[QueryScore]) -> dict:
        if not subset:
            return {"queries": 0}
        n = len(subset)
        return {
            "queries": n,
            # Both are reported so a reader can see what recall is measured against:
            # avg_primary_docs is recall's denominator, avg_gold_docs is nDCG's.
            "avg_gold_docs": round(sum(s.gold_size for s in subset) / n, 2),
            "avg_primary_docs": round(sum(s.primary_size for s in subset) / n, 2),
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


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> list[float]:
    """95% Wilson score interval for a proportion — correct near 0 and 1, unlike normal.

    Reported next to every rate so a reader sees the resolution of the sample instead
    of ranking configs whose intervals overlap almost entirely.
    """
    if total <= 0:
        return [0.0, 0.0]
    p = successes / total
    denominator = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denominator
    margin = (z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))) / denominator
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def _binomial_two_sided(wins: int, trials: int) -> float:
    """Exact two-sided binomial p-value at p=0.5 (no scipy)."""
    if trials == 0:
        return 1.0
    coefficient = 1
    probabilities = []
    for k in range(trials + 1):
        probabilities.append(coefficient * 0.5**trials)
        coefficient = coefficient * (trials - k) // (k + 1)
    observed = probabilities[wins]
    # sum every outcome at most as likely as the observed one (with float slack)
    return min(1.0, sum(p for p in probabilities if p <= observed * (1 + 1e-9)))


def mcnemar(treatment: list[bool], reference: list[bool]) -> dict:
    """Exact McNemar test on paired binary outcomes (same queries, two configs).

    Only discordant pairs carry information: agreement tells you nothing about which
    config is better. Returns the discordant counts and an exact p-value, so a
    reported win is a claim the data can support.
    """
    if len(treatment) != len(reference):
        raise ValueError(f"unpaired samples: {len(treatment)} vs {len(reference)}")
    treatment_only = sum(1 for t, r in zip(treatment, reference) if t and not r)
    reference_only = sum(1 for t, r in zip(treatment, reference) if r and not t)
    discordant = treatment_only + reference_only
    p_value = _binomial_two_sided(treatment_only, discordant)
    return {
        "n": len(treatment),
        "treatment_only": treatment_only,
        "reference_only": reference_only,
        "discordant": discordant,
        "net": treatment_only - reference_only,
        "p_value": round(p_value, 4),
        "significant": p_value < 0.05,
    }


def paired_bootstrap(
    treatment: list[float],
    reference: list[float],
    *,
    iterations: int = 2000,
    seed: int = 42,
) -> dict:
    """95% bootstrap CI for the mean per-query delta (treatment − reference), paired.

    Both lists must be aligned on the same queries in the same order. Deterministic
    (seeded), so reports are reproducible. ``significant`` means the CI excludes zero
    — the honest reading of "this lift is real, not query-sampling noise".
    """
    if len(treatment) != len(reference):
        raise ValueError(f"unpaired samples: {len(treatment)} vs {len(reference)}")
    n = len(treatment)
    if n == 0:
        return {"n": 0}
    deltas = [t - r for t, r in zip(treatment, reference)]
    mean = sum(deltas) / n
    rng = random.Random(seed)
    resampled = sorted(
        sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(iterations)
    )
    low = resampled[round(0.025 * (iterations - 1))]
    high = resampled[round(0.975 * (iterations - 1))]
    return {
        "n": n,
        "delta": round(mean, 4),
        "ci95": [round(low, 4), round(high, 4)],
        "significant": low > 0.0 or high < 0.0,
    }
