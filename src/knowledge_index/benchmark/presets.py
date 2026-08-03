"""Query-time retrieval presets: competitor RAG systems and single-knob ablations.

Every knob a preset touches — leg weights, RRF constant, version-status boost,
document collapse, rerank, chunk-kind scope — is read by ``RetrievalService`` at
*query* time from ``config.retrieval``, so the whole matrix runs over **one index**
by handing retrieval a different ``AppConfig`` copy per preset. Nothing re-ingests,
and nothing mutates the caller's config.

Two preset families:

- **Competitors** — the standard RAG comparison ladder, weakest to strongest:
  ``bm25`` → ``naive_dense`` → ``naive_dense_rerank`` → ``hybrid_rrf`` →
  ``hybrid_rrf_rerank`` → ``full``. Generic presets share ``_GENERIC``: no
  identifier leg, no supersession decay, no collapse, and **body chunks only**
  (a generic stack has no profile/clause rows — leaving them in would credit the
  baseline with our ingest features). ``hybrid_rrf_rerank`` is the strongest
  off-the-shelf pipeline and the baseline ``full`` has to beat.
- **Ablations** — leave-one-out from ``full``, one knob reverted per preset, so the
  matrix answers "what does each of our features buy?". The decisions FTS leg is
  *not* here: ``search_semantic`` never fuses it (``search_decisions`` is its own
  tool, exercised by the agentic tier).

``chunk_contextualize`` cannot be ablated query-time (headers are baked into the
embedded chunk text at ingest); it is the one documented gap in the matrix.
"""

from __future__ import annotations

from knowledge_index.config import AppConfig

# A flat boost neutralizes supersession decay so a preset ranks purely on its leg
# scores; the full system keeps the configured executed > final > draft order.
_FLAT_BOOST = {"executed": 1.0, "final": 1.0, "unknown": 1.0, "draft": 1.0}

# What every non-KI baseline shares: none of the legal-specific machinery, and only
# body chunks (no profile/clause rows — those are our ingest features).
_GENERIC = {
    "weight_identifier": 0.0,
    "version_status_boost": _FLAT_BOOST,
    "collapse_per_document": False,
    "search_chunk_kinds": ["chunk"],
    # extracted party metadata in the ranked query is ours; a generic stack has no
    # such field to boost on
    "metadata_boost": 0.0,
}

#: the standard RAG comparison ladder, weakest to strongest; "full" applies nothing
COMPETITORS: dict[str, dict] = {
    "bm25": {**_GENERIC, "weight_lexical": 1.0, "weight_semantic": 0.0, "rerank_enabled": False},
    "naive_dense": {
        **_GENERIC,
        "weight_lexical": 0.0,
        "weight_semantic": 1.0,
        "rerank_enabled": False,
    },
    "naive_dense_rerank": {
        **_GENERIC,
        "weight_lexical": 0.0,
        "weight_semantic": 1.0,
        "rerank_enabled": True,
    },
    "hybrid_rrf": {
        **_GENERIC,
        "weight_lexical": 1.0,
        "weight_semantic": 1.0,
        "rerank_enabled": False,
    },
    "hybrid_rrf_rerank": {
        **_GENERIC,
        "weight_lexical": 1.0,
        "weight_semantic": 1.0,
        "rerank_enabled": True,
    },
    "full": {},
}

#: leave-one-out from ``full`` — each preset reverts exactly one feature
ABLATIONS: dict[str, dict] = {
    "full_no_identifier": {"weight_identifier": 0.0},
    "full_no_lexical": {"weight_lexical": 0.0},
    "full_no_semantic": {"weight_semantic": 0.0},
    "full_no_collapse": {"collapse_per_document": False},
    "full_no_metadata": {"metadata_boost": 0.0},
    "full_no_statusboost": {"version_status_boost": _FLAT_BOOST},
    "full_no_profiles": {"search_chunk_kinds": ["chunk", "clause"]},
    "full_no_clauses": {"search_chunk_kinds": ["chunk", "profile"]},
    # full ships rerank_enabled=False, so the ± pair is full vs full_rerank
    "full_rerank": {"rerank_enabled": True},
}

#: the one continuous fusion knob, bracketed around the literature default (60)
SWEEPS: dict[str, dict] = {
    "full_rrfk20": {"fusion_rrf_k": 20},
    "full_rrfk240": {"fusion_rrf_k": 240},
}

PRESETS: dict[str, dict] = {**COMPETITORS, **ABLATIONS, **SWEEPS}

#: named preset groups accepted by ``run-retrieval-eval --presets``
GROUPS: dict[str, tuple[str, ...]] = {
    "competitors": tuple(COMPETITORS),
    "ablations": ("full", *ABLATIONS),
    "sweep": ("full", *SWEEPS),
    "all": tuple(PRESETS),
}


def preset_names() -> list[str]:
    return list(PRESETS)


def resolve_presets(spec: str) -> tuple[str, ...]:
    """Resolve a ``--presets`` spec: a group name or a comma-separated preset list.

    Always includes ``full`` (deduplicated, order preserved) — every comparison and
    the gate are anchored on it.
    """
    names = GROUPS.get(spec) or tuple(name.strip() for name in spec.split(",") if name.strip())
    unknown = [name for name in names if name not in PRESETS]
    if unknown:
        raise KeyError(f"unknown preset(s) {unknown}; known: {sorted(PRESETS)}")
    ordered = list(names) if "full" in names else [*names, "full"]
    seen: set[str] = set()
    return tuple(name for name in ordered if not (name in seen or seen.add(name)))


def apply_preset(config: AppConfig, name: str) -> AppConfig:
    """Return a deep copy of ``config`` with the named preset's overrides applied.

    Raises ``KeyError`` for an unknown preset so a typo fails loud rather than
    silently evaluating the full system.
    """
    if name not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; known: {sorted(PRESETS)}")
    ablated = config.model_copy(deep=True)
    for field, value in PRESETS[name].items():
        setattr(ablated.retrieval, field, value)
    return ablated
