"""The resolution rule, stated as tests: what links automatically, what is proven
distinct, what escalates to the agent — and which role claims are refused.

Written against the 9,288-document run this replaces: 1,212 client rows for 46 real
clients, 985 distinct names carrying role=client including individuals,
counterparties, and the firm itself 16 times.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig, FirmConfig
from knowledge_index.db.models import (
    Client,
    Document,
    Matter,
    MatterClient,
    MatterParty,
    Party,
)
from knowledge_index.pipeline.extraction import ExtractedParty, TypedIdentifier
from knowledge_index.pipeline.matter_search import (
    entity_candidates,
    entity_search_covered,
    link_decision,
    resolve_or_create_entity,
)
from knowledge_index.pipeline.runner import _resolve_document_parties

PROVENANCE = {"method": "inferred", "model": "test-model", "evidence": ["ev-1"]}


def _matter(session: Session, mid: str, ref: str, *, imported: bool = False) -> None:
    session.add(
        Matter(id=mid, title=f"Matter {ref}", reference_numbers=[ref], imported=imported)
    )
    session.commit()


def _doc(session: Session, did: str, matter_id: str) -> Document:
    session.add(Document(id=did, matter_id=matter_id, title="Doc"))
    session.commit()
    return session.get(Document, did)


def _resolve(
    session: Session,
    factory: sessionmaker[Session],
    document: Document,
    parties: list[ExtractedParty],
    *,
    config: AppConfig | None = None,
) -> list[dict]:
    payload = _resolve_document_parties(
        session,
        document,
        parties,
        model="test-model",
        evidence="ev-1",
        session_factory=factory,
        config=config or AppConfig(),
    )
    session.commit()
    return payload


def _create(
    factory: sessionmaker[Session],
    name: str,
    *,
    entity_type: str = "party",
    identifiers: dict[str, str] | None = None,
    matter_id: str | None = None,
) -> str:
    return resolve_or_create_entity(
        factory,
        entity_type=entity_type,
        name=name,
        kind="legal_entity",
        identifiers=identifiers or {},
        provenance=PROVENANCE,
        matter_id=matter_id,
    )["id"]


# --- entity_search_covered: did a query plausibly look for this party? ---


def test_covered_by_exact_and_suffix_stripped_queries() -> None:
    searched = {"Vantage Prime Bank"}
    assert entity_search_covered("Vantage Prime Bank AG", searched)
    assert entity_search_covered("vantage prime bank", searched)
    assert entity_search_covered("Vantage Prime Bank", {"Vantage Prime Bank AG"})


def test_not_covered_by_unrelated_or_absent_queries() -> None:
    assert not entity_search_covered("Meridian Capital Partners LP", set())
    assert not entity_search_covered("Meridian Capital Partners LP", {"Vantage Prime Bank"})
    assert not entity_search_covered(
        "Meridian Health Partners, Inc.", {"Meridian Capital Partners LP"}
    )


def test_punctuation_and_case_do_not_defeat_coverage() -> None:
    assert entity_search_covered("Whitfield & Crane LLP", {"whitfield  crane"})


# --- the rule ---


def test_a_shared_identifier_links_whatever_the_names_say(
    session: Session, factory: sessionmaker[Session]
) -> None:
    existing = _create(factory, "Velthryn Strategic Investments LP", identifiers={"lei": "LEI-1"})
    candidates = entity_candidates(
        session, "Pinnacle Growth Acquisition Corp", identifiers={"lei": "LEI-1"}
    )
    entity, reason, _ = link_decision(
        session, candidates, entity_type="party", matter_id=None, sibling_entity_ids=set()
    )
    assert reason == "shared_identifier"
    assert entity is not None and entity.entity_id == existing


def test_an_identical_normalized_name_links_across_matters(
    session: Session, factory: sessionmaker[Session]
) -> None:
    existing = _create(factory, "Kensington Bank AG")
    candidates = entity_candidates(session, "kensington bank")
    entity, reason, _ = link_decision(
        session, candidates, entity_type="party", matter_id=None, sibling_entity_ids=set()
    )
    assert reason == "normalized_name"
    assert entity is not None and entity.entity_id == existing


def test_a_conflicting_register_number_proves_two_companies(
    session: Session, factory: sessionmaker[Session]
) -> None:
    first = _create(factory, "Meridian Capital", identifiers={"de_hrb": "HRB 111"})
    second = _create(factory, "Meridian Capital", identifiers={"de_hrb": "HRB 222"})
    assert first != second
    assert session.scalar(select(func.count()).select_from(Party)) == 2
    # The split is recorded on the row, not hidden in a comment.
    rows = session.scalars(select(Party).order_by(Party.identity_discriminator)).all()
    assert [row.identity_discriminator for row in rows] == ["", "de_hrb:HRB 222"]
    # ... and the third mention of the second company links by its identifier.
    assert _create(factory, "Meridian Capital", identifiers={"de_hrb": "HRB 222"}) == second


def test_a_containment_match_alone_does_not_link(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _create(factory, "Verimark Hospitality Group Inc.")
    candidates = entity_candidates(session, "Verimark Group")
    assert [item.verdict for item in candidates] == ["likely"]
    entity, reason, _ = link_decision(
        session, candidates, entity_type="party", matter_id=None, sibling_entity_ids=set()
    )
    assert entity is None
    assert reason == "no_confident_candidate"


def test_a_containment_match_links_when_the_matter_corroborates(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    known = _create(factory, "Verimark Hospitality Group Inc.", matter_id="m1")
    session.add(MatterParty(matter_id="m1", party_id=known, role="opposing_party"))
    session.commit()

    candidates = entity_candidates(session, "Verimark Group")
    entity, reason, corroboration = link_decision(
        session, candidates, entity_type="party", matter_id="m1", sibling_entity_ids=set()
    )
    assert entity is not None and entity.entity_id == known
    assert reason == "name_and_corroboration"
    assert corroboration == ["already_on_this_matter"]


def test_creating_alongside_a_candidate_is_recorded_on_the_row(
    session: Session, factory: sessionmaker[Session]
) -> None:
    """'Created a second entity for a name the estate already knows' has to be
    countable, not merely regrettable."""
    _create(factory, "Verimark Hospitality Group Inc.")
    created = _create(factory, "Verimark Group")
    row = session.get(Party, created)
    resolution = (row.provenance or {})["resolution"]
    assert resolution["decision"] == "created"
    assert [item["name"] for item in resolution["shadowed_candidates"]] == [
        "Verimark Hospitality Group Inc."
    ]
    counted = session.scalar(
        select(func.count())
        .select_from(Party)
        .where(func.jsonb_array_length(Party.provenance["resolution"]["shadowed_candidates"]) > 0)
    )
    assert counted == 1


# --- role assignment ---


def test_the_firm_is_never_its_own_client(
    session: Session, factory: sessionmaker[Session]
) -> None:
    config = AppConfig(firm=FirmConfig(name="Whitfield & Crane LLP", aliases=["Whitfield Crane"]))
    _matter(session, "m1", "REF-1")
    payload = _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [
            ExtractedParty(name="Whitfield & Crane LLP", role="client"),
            ExtractedParty(name="Kensington Bank", role="client"),
        ],
        config=config,
    )
    firm, client = payload
    assert firm["role"] == "advisor"
    assert firm["claimed_role"] == "client"
    assert firm["role_refused_because"] == "own_firm"
    assert firm["entity_type"] == "party"
    assert client["role"] == "client"
    assert session.scalars(select(Client.name)).all() == ["Kensington Bank"]


def test_the_firm_filter_is_inert_until_the_firm_is_named(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    payload = _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [ExtractedParty(name="Whitfield & Crane LLP", role="client")],
    )
    assert payload[0]["role"] == "client"


def test_an_existing_party_on_the_matter_cannot_become_its_client(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [ExtractedParty(name="Meridian Capital", role="opposing_party")],
    )
    payload = _resolve(
        session,
        factory,
        _doc(session, "d2", "m1"),
        [ExtractedParty(name="Meridian Capital", role="client")],
    )
    assert payload[0]["role"] == "opposing_party"
    assert payload[0]["role_refused_because"] == "already_a_party_on_this_matter"
    assert session.scalar(select(func.count()).select_from(Client)) == 0


def test_an_imported_matters_client_outranks_the_document(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1", imported=True)
    authoritative = _create(factory, "Kensington Bank AG", entity_type="client")
    session.add(MatterClient(matter_id="m1", client_id=authoritative))
    session.commit()

    payload = _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [
            ExtractedParty(name="Derek Caldwell", role="client", kind="natural_person"),
            ExtractedParty(name="Kensington Bank", role="client"),
        ],
    )
    person, client = payload
    assert person["role"] == "other"
    assert person["role_refused_because"] == "matter_client_is_authoritative"
    assert client["role"] == "client" and client["party_id"] == authoritative
    assert session.scalar(select(func.count()).select_from(MatterClient)) == 1


def test_typed_identifiers_are_promoted_for_the_role_the_mention_got(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    payload = _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [
            ExtractedParty(
                name="Nordwind Energie GmbH",
                role="opposing_party",
                identifiers=[TypedIdentifier(scheme="de_hrb", value="HRB 45678")],
            )
        ],
    )
    party = session.get(Party, payload[0]["party_id"])
    assert party.identifiers == {"de_hrb": "HRB 45678"}
