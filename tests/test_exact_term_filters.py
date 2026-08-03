"""F3/F4/F6: exact-term filters (identifier, party, chunk_kind) + date-trust sort.

Unit-level coverage of the OpenSearch translation and the index body — no live
services. See docs/ontology-v1-improvement-spec.md F3, F4, F6.
"""

from __future__ import annotations

from types import SimpleNamespace

from knowledge_index.mcp_server import _active_filters
from knowledge_index.retrieval_types import SearchFilters
from knowledge_index.search_backend import OpenSearchIndex, _combined_filter


class _AllowAllScope:
    """Minimal CompiledAccessScope stand-in: _combined_filter only calls this."""

    def opensearch_filter(self) -> dict:
        return {"match_all": {}}


def _clauses(filters: SearchFilters) -> list[dict]:
    return _combined_filter(_AllowAllScope(), filters)["bool"]["filter"]


def test_identifier_maps_to_exact_term_on_identifiers_keyword() -> None:
    # F3: the exact identifier filter hits the `identifiers` keyword field (exact),
    # NOT identifiers_text (the fuzzy ranking leg).
    assert {"term": {"identifiers": "4 O 123/26"}} in _clauses(
        SearchFilters(identifier="4 O 123/26")
    )


def test_party_maps_to_exact_term_on_parties_keyword() -> None:
    # F4: party filters the denormalized `parties` keyword (party_id OR canonical name).
    assert {"term": {"parties": "Ostsee Handel AG"}} in _clauses(
        SearchFilters(party="Ostsee Handel AG")
    )
    assert {"term": {"parties": "party-abc123"}} in _clauses(
        SearchFilters(party="party-abc123")
    )


def test_chunk_kind_maps_to_exact_term() -> None:
    # F3: chunk_kind scopes to body/profile/clause chunks.
    assert {"term": {"chunk_kind": "clause"}} in _clauses(
        SearchFilters(chunk_kind="clause")
    )


def test_exact_term_filters_absent_by_default() -> None:
    clauses = _clauses(SearchFilters())
    fields = {next(iter(c["term"])) for c in clauses if "term" in c}
    assert not ({"identifiers", "parties", "chunk_kind"} & fields)


def test_exact_term_filters_compose_and_and_with_each_other() -> None:
    clauses = _clauses(
        SearchFilters(identifier="HRB 45678", party="Nordwind Energie GmbH", chunk_kind="chunk")
    )
    assert {"term": {"identifiers": "HRB 45678"}} in clauses
    assert {"term": {"parties": "Nordwind Energie GmbH"}} in clauses
    assert {"term": {"chunk_kind": "chunk"}} in clauses


def test_active_filters_records_exact_terms_only_when_set() -> None:
    active = _active_filters(SearchFilters(identifier="X", party="Y"))
    assert active["identifier"] == "X"
    assert active["party"] == "Y"
    assert "chunk_kind" not in active  # None → omitted


def test_doc_body_promotes_kind_and_parties_for_indexing() -> None:
    # F3/F4: the index body must carry chunk_kind (out of the non-searchable meta
    # object) and the denormalized parties keyword list.
    chunk = SimpleNamespace(
        text="body", project_id="p", document_id="d", document_version_id="v",
        matter_id="m", doc_type=None, doc_type_ancestors=[], version_status="final",
        language="de", doc_date=None, identifiers=["HRB 45678"],
        parties=["Nordwind Energie GmbH", "party-abc123"],
        allowed_principals=[], denied_principals=[], access_version=1, embedding=[],
        meta={"kind": "clause", "locus": "§ 9"},
    )
    body = OpenSearchIndex.__new__(OpenSearchIndex)._doc_body(chunk)
    assert body["chunk_kind"] == "clause"
    assert body["parties"] == ["Nordwind Energie GmbH", "party-abc123"]
