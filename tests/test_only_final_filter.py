"""F1 only_final: restrict search to authoritative versions (final/executed).

Unit-level coverage of the OpenSearch translation and the audit-log behavior —
no live services. See docs/ontology-v1-improvement-spec.md F1.
"""

from __future__ import annotations

from knowledge_index.mcp_server import _active_filters
from knowledge_index.retrieval_types import SearchFilters
from knowledge_index.search_backend import _combined_filter

_FINAL_CLAUSE = {"terms": {"version_status": ["final", "executed"]}}


class _AllowAllScope:
    """Minimal CompiledAccessScope stand-in: _combined_filter only calls this."""

    def opensearch_filter(self) -> dict:
        return {"match_all": {}}


def _clauses(filters: SearchFilters) -> list[dict]:
    return _combined_filter(_AllowAllScope(), filters)["bool"]["filter"]


def test_only_final_appends_final_executed_terms_clause() -> None:
    assert _FINAL_CLAUSE in _clauses(SearchFilters(only_final=True))


def test_default_leaves_version_status_unconstrained() -> None:
    clauses = _clauses(SearchFilters())
    assert _FINAL_CLAUSE not in clauses
    assert all(c.get("term", {}).get("version_status") is None for c in clauses)


def test_only_final_composes_with_version_status_without_erroring() -> None:
    # draft + only_final: both clauses are present and AND together, so at query
    # time no document matches (nothing is both draft and final/executed) — the
    # contradictory-but-legal case must produce a filter, never raise.
    clauses = _clauses(SearchFilters(version_status="draft", only_final=True))
    assert {"term": {"version_status": "draft"}} in clauses
    assert _FINAL_CLAUSE in clauses


def test_active_filters_omits_only_final_when_false() -> None:
    assert "only_final" not in _active_filters(SearchFilters())


def test_active_filters_records_only_final_when_true() -> None:
    assert _active_filters(SearchFilters(only_final=True))["only_final"] is True
