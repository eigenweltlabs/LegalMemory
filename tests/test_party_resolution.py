"""O5 party/client resolution (_resolve_document_parties): the search-then-
link-or-create writes materialize the firm-wide entity layer, and the same name
across different matters deliberately stays distinct (the entity-collision case
the corpus plants with three different "Meridian" companies).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knowledge_index.db.models import (
    Client,
    Document,
    EntityIdentifier,
    Matter,
    MatterClient,
    MatterParty,
    Party,
)
from knowledge_index.pipeline.billing import resolve_entity
from knowledge_index.pipeline.extraction import ExtractedParty, TypedIdentifier
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


def test_creates_client_and_party_with_links_and_identifiers(session: Session) -> None:
    _matter(session, "m1", "REF-1")
    doc = _doc(session, "d1", "m1")
    payload = _resolve(
        session,
        doc,
        [
            ExtractedParty(name="Kensington Bank", role="client"),
            ExtractedParty(
                name="Meridian Capital",
                role="opposing_party",
                identifiers=[TypedIdentifier(scheme="lei", value="LEI-MER")],
            ),
        ],
    )

    # role=client → clients + matter_clients (NOT parties)
    client = session.scalar(select(Client).where(Client.name == "Kensington Bank"))
    assert client is not None
    assert session.get(MatterClient, ("m1", client.id)) is not None

    # every other role → parties + matter_parties keyed by role
    party = session.scalar(select(Party).where(Party.name == "Meridian Capital"))
    assert party is not None
    assert session.get(MatterParty, ("m1", party.id, "opposing_party")) is not None

    # typed identifier promoted to the shared lookup table
    assert (
        session.scalar(
            select(EntityIdentifier).where(
                EntityIdentifier.entity_id == party.id, EntityIdentifier.value == "LEI-MER"
            )
        )
        is not None
    )

    # payload carries each mention's resolved entity
    assert {row["entity_type"] for row in payload} == {"client", "party"}

    # the loop closes: search_entities (resolve_entity) now finds what we created
    assert any(hit["id"] == party.id for hit in resolve_entity(session, "Meridian"))


def test_same_name_across_matters_stays_distinct(session: Session) -> None:
    # Three matters, three different "Meridian" entities, each created fresh
    # (existing_id=None). The deliberate name collision must NOT merge them.
    ids: list[str] = []
    for i in (1, 2, 3):
        _matter(session, f"m{i}", f"REF-{i}")
        doc = _doc(session, f"d{i}", f"m{i}")
        payload = _resolve(session, doc, [ExtractedParty(name="Meridian", role="opposing_party")])
        ids.append(payload[0]["party_id"])

    assert len(set(ids)) == 3
    assert _party_count(session, "Meridian") == 3


def test_existing_id_links_instead_of_creating(session: Session) -> None:
    _matter(session, "m1", "REF-1")
    _matter(session, "m2", "REF-2")
    first = _resolve(
        session, _doc(session, "d1", "m1"), [ExtractedParty(name="Meridian", role="opposing_party")]
    )
    party_id = first[0]["party_id"]

    # A second document links to the SAME entity by id (the agent recognized it).
    second = _resolve(
        session,
        _doc(session, "d2", "m2"),
        [ExtractedParty(name="Meridian", role="opposing_party", existing_id=party_id)],
    )
    assert second[0]["party_id"] == party_id
    assert _party_count(session, "Meridian") == 1
    assert session.get(MatterParty, ("m2", party_id, "opposing_party")) is not None
