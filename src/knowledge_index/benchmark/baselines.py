"""Baseline retrieval configurations expressed as query-time overrides.

Every knob these presets touch — leg weights, RRF constant, version-status boost,
document collapse, rerank — is read by ``RetrievalService`` at *query* time from
``config.retrieval`` (see ``retrieval.py``). So the whole ladder is a pure
query-time ablation: index the corpus once, then evaluate each preset by handing
``RetrievalService`` a different ``AppConfig`` copy. Nothing here re-ingests, and
nothing mutates the caller's config — every preset returns a deep copy.

``naive_dense`` is the simple baseline the full system is meant to beat: a single
embedding leg, flat cosine top-k, no lexical/identifier legs, no version collapse,
no rerank, no supersession boost. ``bm25`` is the near-free lexical floor beneath
it. ``full`` is the shipped configuration (defaults), the target to exceed.
"""

from __future__ import annotations

from knowledge_index.config import AppConfig

# A flat boost neutralizes supersession decay so a baseline ranks purely on its
# leg scores; the full system keeps the configured executed > final > draft order.
_FLAT_BOOST = {"executed": 1.0, "final": 1.0, "unknown": 1.0, "draft": 1.0}

# name -> retrieval field overrides (query-time only). "full" applies nothing.
BASELINES: dict[str, dict] = {
    "bm25": {
        "weight_lexical": 1.0,
        "weight_semantic": 0.0,
        "weight_identifier": 0.0,
        "weight_decisions": 0.0,
        "version_status_boost": _FLAT_BOOST,
        "collapse_per_document": False,
        "rerank_enabled": False,
    },
    "naive_dense": {
        "weight_lexical": 0.0,
        "weight_semantic": 1.0,
        "weight_identifier": 0.0,
        "weight_decisions": 0.0,
        "version_status_boost": _FLAT_BOOST,
        "collapse_per_document": False,
        "rerank_enabled": False,
    },
    "full": {},
}

#: default ablation ladder, weakest to strongest
LADDER: tuple[str, ...] = ("bm25", "naive_dense", "full")


def baseline_names() -> list[str]:
    return list(BASELINES)


def apply_baseline(config: AppConfig, name: str) -> AppConfig:
    """Return a deep copy of ``config`` with the named baseline's overrides applied.

    Raises ``KeyError`` for an unknown baseline so a typo fails loud rather than
    silently evaluating the full system.
    """
    if name not in BASELINES:
        raise KeyError(f"unknown baseline {name!r}; known: {sorted(BASELINES)}")
    ablated = config.model_copy(deep=True)
    for field, value in BASELINES[name].items():
        setattr(ablated.retrieval, field, value)
    return ablated
