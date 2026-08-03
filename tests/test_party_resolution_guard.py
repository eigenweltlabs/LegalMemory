"""Entity-resolution forcing (2026-08-01 run audit): a CREATE is only accepted
when the agent actually searched for the party, and a same-matter verbatim name
reuses the known entity instead of minting a twin — while same-name entities in
different matters stay distinct (the deliberate Meridian collision case).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knowledge_index.db.models import Client, Document, Matter, MatterParty, Party
from knowledge_index.pipeline.extraction import ExtractedParty
from knowledge_index.pipeline.matter_search import entity_search_covered
from knowledge_index.pipeline.runner import _resolve_document_parties


def _matter(session: Session, mid: str, ref: str) -> None:
    session.add(Matter(id=mid, title=f"Matter {ref}", reference_numbers=[ref]))
    session.flush()


def _doc(session: Session, did: str, matter_id: str) -> Document:
    session.add(Document(id=did, matter_id=matter_id, title="Doc"))
    session.flush()
    return session.get(Document, did)


def _resolve(session: Session, document: Document, parties: list[ExtractedParty]) -> list[dict]:
    payload = _resolve_document_parties(
        session, document, parties, model="test-model", evidence="ev-1"
    )
    session.flush()
    return payload


def _party_count(session: Session, name: str) -> int:
    return session.scalar(select(func.count()).select_from(Party).where(Party.name == name))


# --- entity_search_covered: did a query plausibly look for this party? ---


def test_covered_by_exact_and_suffix_stripped_queries() -> None:
    searched = {"Vantage Prime Bank"}
    assert entity_search_covered("Vantage Prime Bank AG", searched)
    assert entity_search_covered("vantage prime bank", searched)
    # normalization is symmetric: searching the fuller form covers the shorter name
    assert entity_search_covered("Vantage Prime Bank", {"Vantage Prime Bank AG"})


def test_not_covered_by_unrelated_or_absent_queries() -> None:
    assert not entity_search_covered("Meridian Capital Partners LP", set())
    assert not entity_search_covered(
        "Meridian Capital Partners LP", {"Vantage Prime Bank"}
    )
    # sharing one token is not coverage — each party needs its own search
    assert not entity_search_covered(
        "Meridian Health Partners, Inc.", {"Meridian Capital Partners LP"}
    )


def test_punctuation_and_case_do_not_defeat_coverage() -> None:
    assert entity_search_covered(
        "Whitfield & Crane LLP", {"whitfield  crane"}
    )


# --- same-matter verbatim-name net in _resolve_document_parties ---


def test_same_matter_same_name_reuses_entity(session: Session) -> None:
    _matter(session, "m1", "REF-1")
    first = _resolve(
        session, _doc(session, "d1", "m1"), [ExtractedParty(name="Meridian", role="opposing_party")]
    )
    # The agent "forgot" and decided create again for the identical name.
    second = _resolve(
        session, _doc(session, "d2", "m1"), [ExtractedParty(name="Meridian", role="opposing_party")]
    )
    assert second[0]["party_id"] == first[0]["party_id"]
    assert _party_count(session, "Meridian") == 1


def test_net_is_case_insensitive_and_spans_roles(session: Session) -> None:
    _matter(session, "m1", "REF-1")
    first = _resolve(
        session, _doc(session, "d1", "m1"), [ExtractedParty(name="Meridian Capital", role="advisor")]
    )
    second = _resolve(
        session,
        _doc(session, "d2", "m1"),
        [ExtractedParty(name="  MERIDIAN CAPITAL ", role="opposing_party")],
    )
    assert second[0]["party_id"] == first[0]["party_id"]
    # the second role still gets its own matter link on the shared entity
    assert session.get(MatterParty, ("m1", first[0]["party_id"], "advisor")) is not None
    assert session.get(MatterParty, ("m1", first[0]["party_id"], "opposing_party")) is not None


def test_net_applies_to_clients_too(session: Session) -> None:
    _matter(session, "m1", "REF-1")
    first = _resolve(
        session, _doc(session, "d1", "m1"), [ExtractedParty(name="Kensington Bank", role="client")]
    )
    second = _resolve(
        session, _doc(session, "d2", "m1"), [ExtractedParty(name="Kensington Bank", role="client")]
    )
    assert second[0]["party_id"] == first[0]["party_id"]
    assert (
        session.scalar(
            select(func.count()).select_from(Client).where(Client.name == "Kensington Bank")
        )
        == 1
    )


def test_cross_matter_same_name_still_stays_distinct(session: Session) -> None:
    # The net must NOT merge across matters: different companies share names.
    ids = []
    for i in (1, 2):
        _matter(session, f"m{i}", f"REF-{i}")
        payload = _resolve(
            session,
            _doc(session, f"d{i}", f"m{i}"),
            [ExtractedParty(name="Meridian", role="opposing_party")],
        )
        ids.append(payload[0]["party_id"])
    assert len(set(ids)) == 2
    assert _party_count(session, "Meridian") == 2


def test_variant_names_in_one_matter_stay_distinct(session: Session) -> None:
    # Verbatim-only matching: "Inc." vs "LLC" variants are NOT collapsed by the
    # net — that judgment stays with the agent via search_entities.
    _matter(session, "m1", "REF-1")
    _resolve(
        session,
        _doc(session, "d1", "m1"),
        [ExtractedParty(name="Meridian Health Partners, Inc.", role="advisor")],
    )
    _resolve(
        session,
        _doc(session, "d2", "m1"),
        [ExtractedParty(name="Meridian Health Partners, LLC", role="advisor")],
    )
    assert _party_count(session, "Meridian Health Partners, Inc.") == 1
    assert _party_count(session, "Meridian Health Partners, LLC") == 1
