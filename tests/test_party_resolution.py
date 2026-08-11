"""O5 party/client resolution (_resolve_document_parties): what the search-then-
link-or-create writes materialize, and that one real-world entity ends up as one row
however many matters and spellings it arrives under.

The corpus this replaces was measured with 1,212 client rows for 46 real clients,
1,076 of them touching exactly one matter. Cross-matter identity is therefore the
default here, not a judgement the agent may decline.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
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


def _count(session: Session, model, name: str) -> int:
    return session.scalar(select(func.count()).select_from(model).where(model.name == name))


def test_creates_client_and_party_with_links_and_identifiers(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    doc = _doc(session, "d1", "m1")
    payload = _resolve(
        session,
        factory,
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

    # payload carries each mention's resolved entity and how it got there
    assert {row["entity_type"] for row in payload} == {"client", "party"}
    assert {row["resolution"] for row in payload} == {"no_confident_candidate"}

    # the loop closes: entity resolution now finds what we created
    assert any(hit["id"] == party.id for hit in resolve_entity(session, "Meridian"))


def test_one_client_across_many_matters_is_one_row(
    session: Session, factory: sessionmaker[Session]
) -> None:
    """The shape a firm actually has: a client with five matters, not five clients."""
    ids = []
    for index in (1, 2, 3, 4, 5):
        _matter(session, f"m{index}", f"REF-{index}")
        payload = _resolve(
            session,
            factory,
            _doc(session, f"d{index}", f"m{index}"),
            [ExtractedParty(name="Kensington Bank AG", role="client")],
        )
        ids.append(payload[0]["party_id"])

    assert len(set(ids)) == 1
    assert session.scalar(select(func.count()).select_from(Client)) == 1
    assert (
        len(
            session.scalars(
                select(MatterClient.matter_id).where(MatterClient.client_id == ids[0])
            ).all()
        )
        == 5
    )


def test_name_variants_converge_and_are_recorded_as_aliases(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    _matter(session, "m2", "REF-2")
    first = _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [ExtractedParty(name="Nordwind Energie GmbH", role="opposing_party")],
    )
    second = _resolve(
        session,
        factory,
        _doc(session, "d2", "m2"),
        [ExtractedParty(name="Nordwind Energie", role="opposing_party")],
    )
    assert second[0]["party_id"] == first[0]["party_id"]
    assert session.scalar(select(func.count()).select_from(Party)) == 1

    # Same identity, different surface form → the second spelling is now searchable.
    party = session.get(Party, first[0]["party_id"])
    session.refresh(party)
    assert party.aliases == []  # "Nordwind Energie" normalizes to the stored key itself
    third = _resolve(
        session,
        factory,
        _doc(session, "d3", "m2"),
        [ExtractedParty(name="Nordwind Energie GmbH, Hamburg", role="opposing_party")],
    )
    assert third[0]["party_id"] == first[0]["party_id"]
    session.refresh(party)
    assert "Nordwind Energie GmbH, Hamburg" in party.aliases
    assert "nordwind energie hamburg" in party.normalized_aliases


def test_existing_id_links_instead_of_creating(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    _matter(session, "m2", "REF-2")
    first = _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [ExtractedParty(name="Meridian", role="opposing_party")],
    )
    party_id = first[0]["party_id"]

    second = _resolve(
        session,
        factory,
        _doc(session, "d2", "m2"),
        [ExtractedParty(name="Meridian", role="opposing_party", existing_id=party_id)],
    )
    assert second[0]["party_id"] == party_id
    assert _count(session, Party, "Meridian") == 1
    assert session.get(MatterParty, ("m2", party_id, "opposing_party")) is not None


def test_one_entity_that_is_client_here_and_counterparty_there(
    session: Session, factory: sessionmaker[Session]
) -> None:
    """Clients and parties are separate tables, so the pair has to be recognizable
    as one entity rather than as two unrelated ones."""
    _matter(session, "m1", "REF-1")
    _matter(session, "m2", "REF-2")
    _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [
            ExtractedParty(
                name="Kensington Bank",
                role="opposing_party",
                identifiers=[TypedIdentifier(scheme="lei", value="LEI-KEN")],
            )
        ],
    )
    payload = _resolve(
        session,
        factory,
        _doc(session, "d2", "m2"),
        [ExtractedParty(name="Kensington Bank", role="client")],
    )
    client = session.get(Client, payload[0]["party_id"])
    assert client is not None
    assert client.normalized_name == "kensington bank"
    # the identifier travelled with the identity
    assert session.scalar(
        select(EntityIdentifier).where(
            EntityIdentifier.entity_type == "client",
            EntityIdentifier.entity_id == client.id,
            EntityIdentifier.value == "LEI-KEN",
        )
    )
    assert session.scalar(select(func.count()).select_from(Client)) == 1
    assert session.scalar(select(func.count()).select_from(Party)) == 1


def test_a_document_naming_one_party_twice_writes_one_link(
    session: Session, factory: sessionmaker[Session]
) -> None:
    _matter(session, "m1", "REF-1")
    payload = _resolve(
        session,
        factory,
        _doc(session, "d1", "m1"),
        [
            ExtractedParty(name="Kensington Bank AG", role="client"),
            ExtractedParty(name="Kensington Bank", role="client"),
        ],
    )
    assert payload[0]["party_id"] == payload[1]["party_id"]
    assert session.scalar(select(func.count()).select_from(MatterClient)) == 1
