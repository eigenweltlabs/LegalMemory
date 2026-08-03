"""An empty filtered search must say why, not just return nothing.

Measured on real agent traffic: 41 filter calls returned an empty list and the
caller almost never recovered — it re-guessed or abandoned the matter. These
cover the advisory that replaces that silence.
"""

from __future__ import annotations

from types import SimpleNamespace

from knowledge_index.mcp_server import _with_empty_result_help
from knowledge_index.retrieval_types import SearchFilters
from knowledge_index.search_backend import _looks_like_id


def _retrieval(suggestions: dict | Exception):
    def suggest_for_empty(**_kwargs):
        if isinstance(suggestions, Exception):
            raise suggestions
        return suggestions

    return SimpleNamespace(suggest_for_empty=suggest_for_empty)


def test_hits_are_returned_untouched() -> None:
    hits = [{"citations": [{"document": {"id": "d1"}}]}]
    assert _with_empty_result_help(hits, _retrieval({}), set(), SearchFilters(party="X")) is hits


def test_an_unfiltered_empty_search_stays_empty() -> None:
    # Nothing was constrained, so there is nothing to suggest relaxing.
    assert _with_empty_result_help([], _retrieval({"party": ["A"]}), set(), SearchFilters()) == []


def test_near_misses_are_offered_back() -> None:
    out = _with_empty_result_help(
        [],
        _retrieval({"party": ["Whitfield & Crane LLP", "Whitfield Capital Partners LLC"]}),
        set(),
        SearchFilters(party="Huang-Whitfield"),
    )
    assert len(out) == 1
    assert out[0]["no_results"] is True
    assert out[0]["filters_applied"] == ["party"]
    assert out[0]["did_you_mean"]["party"][0] == "Whitfield & Crane LLP"
    # Must not be mistakable for a hit: the server forbids claims without citations.
    assert "citations" not in out[0]


def test_no_near_miss_says_the_value_is_not_an_identifier() -> None:
    """'Lumenex' is a company name the model put in identifier=. Say so."""
    out = _with_empty_result_help(
        [], _retrieval({}), set(), SearchFilters(identifier="Lumenex")
    )
    assert len(out) == 1
    assert "did_you_mean" not in out[0]
    assert "not a legal identifier" in out[0]["message"]
    assert "identifier" in out[0]["message"]


def test_a_failing_suggestion_never_breaks_the_search() -> None:
    assert _with_empty_result_help(
        [], _retrieval(RuntimeError("opensearch down")), set(), SearchFilters(party="X")
    ) == []


def test_entity_ids_are_never_suggested_as_names() -> None:
    # `parties` mixes resolved ids with canonical names; only names help a caller.
    assert _looks_like_id("0a20530f-b248-455a-8480-fc636074ea31")
    assert not _looks_like_id("Whitfield & Crane LLP")
    assert not _looks_like_id("CPSC-2023-0847")
