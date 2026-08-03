"""F5 practice_area filter (Option B: resolve on the read path, no chunk denormalization).

practice_area lives on the Matter, not on the chunk, so RetrievalService translates it
into the set of matters it covers (SUBTREE semantics) and hands the backend a matter-id
terms filter. Two levels of coverage here, both unit-level (no live services):

  * the backend translation of ``matter_ids`` (incl. the meaningful empty set), and
  * ``RetrievalService._resolve_practice_area`` (subtree match, intersection, clearing).

See docs/ontology-v1-improvement-spec.md F5.
"""

from __future__ import annotations

from types import SimpleNamespace

from knowledge_index.mcp_server import _active_filters
from knowledge_index.retrieval import RetrievalService
from knowledge_index.retrieval_types import SearchFilters
from knowledge_index.search_backend import _combined_filter


class _AllowAllScope:
    """Minimal CompiledAccessScope stand-in: _combined_filter only calls this."""

    def opensearch_filter(self) -> dict:
        return {"match_all": {}}


def _combined(filters: SearchFilters) -> dict:
    return _combined_filter(_AllowAllScope(), filters)


def _clauses(filters: SearchFilters) -> list[dict]:
    return _combined(filters)["bool"]["filter"]


# --- backend: matter_ids translation --------------------------------------------------


def test_matter_ids_maps_to_terms_on_matter_id() -> None:
    assert {"terms": {"matter_id": ["m1", "m2"]}} in _clauses(
        SearchFilters(matter_ids=["m1", "m2"])
    )


def test_empty_matter_ids_is_no_hits_not_no_filter() -> None:
    # An empty set means "the practice_area matched no matter" — it must return nothing,
    # NOT be ignored (which would silently widen to every matter).
    assert _combined(SearchFilters(matter_ids=[])) == {"match_none": {}}


def test_matter_ids_absent_by_default() -> None:
    clauses = _clauses(SearchFilters())
    assert all("terms" not in c or "matter_id" not in c.get("terms", {}) for c in clauses)


def test_matter_ids_composes_and_with_single_matter_id() -> None:
    clauses = _clauses(SearchFilters(matter_id="m1", matter_ids=["m1", "m2"]))
    assert {"term": {"matter_id": "m1"}} in clauses
    assert {"terms": {"matter_id": ["m1", "m2"]}} in clauses


# --- retrieval: practice_area resolution ----------------------------------------------


class _FakeArea:
    """Area-of-Law scope stub. Tree: parent ``A`` -> child ``A.1``; ``B`` is unrelated."""

    _ANCESTORS = {"A": {"A"}, "A.1": {"A.1", "A"}, "B": {"B"}}

    def ancestors(self, node: str) -> set[str]:
        return self._ANCESTORS.get(node, {node})


class _FakeConfig:
    def __init__(self, area: object | None = _FakeArea()) -> None:
        self._area = area

    def ontology_facet(self, facet: str) -> object:
        assert facet == "area_of_law"
        if self._area is None:
            raise ValueError("area_of_law facet inactive")
        return self._area


class _FakeSession:
    """Returns fixed (matter_id, practice_area) rows for the resolver's SELECT."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def execute(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: list(self._rows))


def _resolver(rows: list[tuple[str, str]], *, area: object | None = _FakeArea()) -> RetrievalService:
    service = RetrievalService.__new__(RetrievalService)
    service.session = _FakeSession(rows)  # type: ignore[attr-defined]
    service.config = _FakeConfig(area)  # type: ignore[attr-defined]
    return service


_ROWS = [("m_child", "A.1"), ("m_parent", "A"), ("m_other", "B")]


def test_no_practice_area_is_a_passthrough() -> None:
    original = SearchFilters(doc_type="agreements")
    assert _resolver(_ROWS)._resolve_practice_area(original) is original


def test_practice_area_resolves_to_its_subtree_matters() -> None:
    resolved = _resolver(_ROWS)._resolve_practice_area(SearchFilters(practice_area="A"))
    # A matches itself (m_parent) and its descendant A.1 (m_child); B is outside the subtree.
    assert set(resolved.matter_ids or []) == {"m_child", "m_parent"}
    # practice_area is consumed here — the backend only ever sees matter_ids.
    assert resolved.practice_area is None


def test_practice_area_matching_no_matter_yields_empty_not_none() -> None:
    # "C" covers no matter → empty list → backend returns no hits (see the empty-set test).
    resolved = _resolver(_ROWS)._resolve_practice_area(SearchFilters(practice_area="C"))
    assert resolved.matter_ids == []


def test_practice_area_intersects_with_a_preexisting_matter_id_set() -> None:
    resolved = _resolver(_ROWS)._resolve_practice_area(
        SearchFilters(practice_area="A", matter_ids=["m_child"])
    )
    assert resolved.matter_ids == ["m_child"]


def test_inactive_area_facet_yields_no_matters() -> None:
    resolved = _resolver(_ROWS, area=None)._resolve_practice_area(SearchFilters(practice_area="A"))
    assert resolved.matter_ids == []


# --- audit ----------------------------------------------------------------------------


def test_active_filters_records_practice_area_when_set() -> None:
    assert _active_filters(SearchFilters(practice_area="A"))["practice_area"] == "A"
    assert "practice_area" not in _active_filters(SearchFilters())
