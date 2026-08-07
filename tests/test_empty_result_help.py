"""An empty filtered search must say why, not just return nothing.

Measured on real agent traffic: 41 filter calls returned an empty list and the
caller almost never recovered — it re-guessed or abandoned the matter. These
cover the advisory that replaces that silence.
"""

from __future__ import annotations

from types import SimpleNamespace

from knowledge_index.mcp_server import _empty_result_help
from knowledge_index.retrieval_types import Page, SearchFilters
from knowledge_index.search_backend import _looks_like_id


def _retrieval(suggestions: dict | Exception):
    def suggest_for_empty(**_kwargs):
        if isinstance(suggestions, Exception):
            raise suggestions
        return suggestions

    return SimpleNamespace(suggest_for_empty=suggest_for_empty)


def _page(items: list[dict], *, offset: int = 0) -> Page:
    return Page(items=items, offset=offset, limit=20)


def test_a_page_with_hits_gets_no_advisory() -> None:
    hits = _page([{"citations": [{"document": {"id": "d1"}}]}])
    assert _empty_result_help(hits, _retrieval({}), set(), SearchFilters(party="X")) == {}


def test_an_unfiltered_empty_search_stays_empty() -> None:
    # Nothing was constrained, so there is nothing to suggest relaxing.
    assert (
        _empty_result_help(_page([]), _retrieval({"party": ["A"]}), set(), SearchFilters())
        == {}
    )


def test_near_misses_are_offered_back() -> None:
    out = _empty_result_help(
        _page([]),
        _retrieval({"party": ["Whitfield & Crane LLP", "Whitfield Capital Partners LLC"]}),
        set(),
        SearchFilters(party="Huang-Whitfield"),
    )
    advisory = out["no_results"]
    assert advisory["filters_applied"] == ["party"]
    assert advisory["did_you_mean"]["party"][0] == "Whitfield & Crane LLP"
    # Must not be mistakable for a hit: it is a sibling of `results`, not a row
    # in it, and it carries no citations.
    assert "citations" not in advisory


def test_no_near_miss_says_the_value_is_not_an_identifier() -> None:
    """'Lumenex' is a company name the model put in identifier=. Say so."""
    out = _empty_result_help(
        _page([]), _retrieval({}), set(), SearchFilters(identifier="Lumenex")
    )
    advisory = out["no_results"]
    assert "did_you_mean" not in advisory
    assert "not a legal identifier" in advisory["message"]
    assert "identifier" in advisory["message"]


def test_a_failing_suggestion_never_breaks_the_search() -> None:
    assert (
        _empty_result_help(
            _page([]),
            _retrieval(RuntimeError("opensearch down")),
            set(),
            SearchFilters(party="X"),
        )
        == {}
    )


def test_paging_past_the_end_is_not_advised_about() -> None:
    """An empty page at offset>0 means the caller reached the end of a result set
    that DID match — suggesting other filter values there would be wrong."""
    assert (
        _empty_result_help(
            _page([], offset=20),
            _retrieval({"party": ["Whitfield & Crane LLP"]}),
            set(),
            SearchFilters(party="Whitfield"),
        )
        == {}
    )


def test_entity_ids_are_never_suggested_as_names() -> None:
    # `parties` mixes resolved ids with canonical names; only names help a caller.
    assert _looks_like_id("0a20530f-b248-455a-8480-fc636074ea31")
    assert not _looks_like_id("Whitfield & Crane LLP")
    assert not _looks_like_id("CPSC-2023-0847")
