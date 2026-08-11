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


def test_only_final_is_not_translated_into_a_version_status_clause() -> None:
    """only_final selects a VERSION; it is not a per-chunk term filter.

    `version_status in {final, executed}` looks like the same thing and is not:
    it hides a document whose only version is a draft, even though nothing
    supersedes it, so a matter reports fewer documents than it holds and the
    caller has no way to see why. Authority is a fact about a document's other
    versions, which a chunk-level filter cannot know — it is applied after
    materialization instead, in RetrievalService._drop_superseded.
    """
    assert _FINAL_CLAUSE not in _clauses(SearchFilters(only_final=True))


def test_default_leaves_version_status_unconstrained() -> None:
    clauses = _clauses(SearchFilters())
    assert _FINAL_CLAUSE not in clauses
    assert all(c.get("term", {}).get("version_status") is None for c in clauses)


def test_an_explicit_version_status_is_still_a_clause() -> None:
    # version_status is a genuine per-chunk fact and stays in the query; asking
    # for drafts AND only_final is legal and must produce a filter, never raise.
    clauses = _clauses(SearchFilters(version_status="draft", only_final=True))
    assert {"term": {"version_status": "draft"}} in clauses
    assert _FINAL_CLAUSE not in clauses


def test_active_filters_omits_only_final_when_false() -> None:
    assert "only_final" not in _active_filters(SearchFilters())


def test_active_filters_records_only_final_when_true() -> None:
    assert _active_filters(SearchFilters(only_final=True))["only_final"] is True
