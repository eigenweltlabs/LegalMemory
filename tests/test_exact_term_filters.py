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


def _shoulds(filters: SearchFilters, field: str) -> list[dict]:
    """The should-arms of the one clause that filters ``field``."""
    for clause in _clauses(filters):
        arms = (clause.get("bool") or {}).get("should") or []
        if any(field in next(iter(arm.values())) for arm in arms):
            return arms
    return []


def test_identifier_filter_accepts_the_value_as_written() -> None:
    # F3: the identifier filter targets `identifiers` (exact) with a phrase fallback
    # onto identifiers_text, whose standard analyser absorbs case and punctuation —
    # a raw keyword term alone was too strict to be usable from a tool call.
    arms = _shoulds(SearchFilters(identifier="4 O 123/26"), "identifiers")
    assert {"term": {"identifiers": "4 O 123/26"}} in arms
    assert {"match_phrase": {"identifiers_text": "4 O 123/26"}} in arms


def test_party_filter_matches_a_short_form_of_a_canonical_name() -> None:
    """F4: `parties` holds BOTH entity ids and canonical names.

    An exact term only serves a caller who knows which of the two a document
    stored, spelled in full. Real agent calls do not: 16 of 25 party filters that
    returned nothing passed a short form ("Thornton" for "Thornton & Associates
    LLP"). The id still matches exactly; the name matches loosely.
    """
    arms = _shoulds(SearchFilters(party="Thornton"), "parties")
    assert {"term": {"parties": {"value": "Thornton", "case_insensitive": True}}} in arms
    assert {"prefix": {"parties": {"value": "Thornton", "case_insensitive": True}}} in arms
    assert {
        "wildcard": {"parties": {"value": "*Thornton*", "case_insensitive": True}}
    } in arms


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
    """Each filter contributes its own clause, and they AND together.

    identifier and party each widened into a should-bool; chunk_kind stays a plain
    term. All three still sit side by side in the top-level filter list, so setting
    two narrows rather than widens.
    """
    filters = SearchFilters(
        identifier="HRB 45678", party="Nordwind Energie GmbH", chunk_kind="chunk"
    )
    clauses = _clauses(filters)
    assert {"term": {"chunk_kind": "chunk"}} in clauses
    assert {"term": {"identifiers": "HRB 45678"}} in _shoulds(filters, "identifiers")
    assert {
        "term": {"parties": {"value": "Nordwind Energie GmbH", "case_insensitive": True}}
    } in _shoulds(filters, "parties")
    # three separate filter clauses, not one merged should that would OR them
    assert len(clauses) >= 3


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


def test_named_entities_picks_party_names_not_question_words() -> None:
    """The lexical leg boosts chunks whose extracted parties match a name in the query.

    Capitalisation is the signal, so the words that open a question have to be
    excluded — otherwise every request would boost on its own first word.
    """
    from knowledge_index.search_backend import _named_entities

    assert _named_entities("What's the standstill in the Kosar settlement?") == ["Kosar"]
    assert _named_entities("Who mediated the Hargrove Whitfield matter?") == [
        "Hargrove",
        "Whitfield",
    ]
    # no capitalised content words at all -> nothing to boost on
    assert _named_entities("what is the effective date") == []
    # bounded, and deduplicated
    assert len(_named_entities("Alpha Bravo Charlie Delta Echo Foxtrot")) == 4
    assert _named_entities("Meridian and Meridian again") == ["Meridian"]


def test_metadata_boost_is_ablatable_and_off_for_generic_baselines() -> None:
    from knowledge_index.benchmark import presets
    from knowledge_index.config import AppConfig

    config = AppConfig()
    assert config.retrieval.metadata_boost > 0  # shipped on
    assert presets.apply_preset(config, "full_no_metadata").retrieval.metadata_boost == 0.0
    # a generic stack has no extracted party field to boost on
    assert presets.apply_preset(config, "hybrid_rrf").retrieval.metadata_boost == 0.0
    assert presets.apply_preset(config, "bm25").retrieval.metadata_boost == 0.0
