"""Concurrent creation of the same entity, actually raced.

30% of the duplicate client rows on the 9,288-document run (293 of 973) were created
within 60 seconds of the row they duplicated: documents extracting in parallel each
searched, each was told nothing existed, and each created. A test that calls the
resolver twice in sequence cannot see that bug, so these run real threads against the
real database and assert on the row count afterwards.

Three independent guards, tested one at a time:
the short committed transaction (the entity is visible the moment it exists), the
advisory lock on the normalized name (search-then-create is atomic), and the unique
constraint (whatever gets past the first two loses the insert and re-reads the winner).
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.db.models import Client, Party
from knowledge_index.pipeline import matter_search
from knowledge_index.pipeline.matter_search import resolve_or_create_entity

PROVENANCE = {"method": "inferred", "model": "test-model", "evidence": ["ev-1"]}

# One name, written the way twelve different documents would write it. All of them
# are the same company and must end up as one row.
VARIANTS = [
    "Velthryn Strategic Investments, LP",
    "Velthryn Strategic Investments LP",
    "VELTHRYN STRATEGIC INVESTMENTS, L.P.",
    "Velthryn Strategic Investments",
] * 3


def _race(factory: sessionmaker[Session], names: list[str], entity_type: str) -> list[dict]:
    """Run one resolve per thread, all released at the same instant."""
    start = threading.Barrier(len(names))
    results: list[dict | BaseException] = [None] * len(names)

    def run(index: int) -> None:
        try:
            start.wait(timeout=30)
            results[index] = resolve_or_create_entity(
                factory,
                entity_type=entity_type,
                name=names[index],
                kind="legal_entity",
                identifiers={},
                provenance=PROVENANCE,
            )
        except BaseException as error:  # noqa: BLE001 - re-raised below with context
            results[index] = error

    threads = [threading.Thread(target=run, args=(index,)) for index in range(len(names))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    failures = [item for item in results if isinstance(item, BaseException)]
    if failures:
        raise failures[0]
    return results


def test_twelve_documents_naming_one_new_entity_create_one_row(
    factory: sessionmaker[Session],
) -> None:
    results = _race(factory, VARIANTS, "client")

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Client)) == 1
        duplicates = session.execute(
            select(Client.normalized_name, func.count())
            .group_by(Client.normalized_name)
            .having(func.count() > 1)
        ).all()
        assert duplicates == []
    assert len({item["id"] for item in results}) == 1
    assert sum(1 for item in results if item["created"]) == 1


def test_racing_creations_of_different_entities_all_land(
    factory: sessionmaker[Session],
) -> None:
    """The lock is per name, not global: unrelated entities must not serialize away."""
    names = [f"Harrowgate Ventures {index}" for index in range(8)]
    results = _race(factory, names, "party")

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Party)) == 8
    assert len({item["id"] for item in results}) == 8


def test_the_constraint_refuses_a_twin_even_without_the_lock(
    session: Session, factory: sessionmaker[Session]
) -> None:
    """The schema, not the code path, is what makes a duplicate impossible."""
    resolve_or_create_entity(
        factory,
        entity_type="client",
        name="Kensington Bank AG",
        kind="legal_entity",
        identifiers={},
        provenance=PROVENANCE,
    )
    session.add(Client(name="kensington bank"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_a_creator_that_loses_the_insert_reads_the_winner(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery path itself, forced: with linking disabled every call tries to
    insert, and the second one has to come back with the first one's id rather than
    raise."""
    monkeypatch.setattr(
        matter_search,
        "link_decision",
        lambda *args, **kwargs: (None, "no_confident_candidate", []),
    )
    first = resolve_or_create_entity(
        factory,
        entity_type="party",
        name="Harrowgate Ventures",
        kind="legal_entity",
        identifiers={},
        provenance=PROVENANCE,
    )
    second = resolve_or_create_entity(
        factory,
        entity_type="party",
        name="Harrowgate Ventures Ltd",
        kind="legal_entity",
        identifiers={},
        provenance=PROVENANCE,
    )
    assert second["id"] == first["id"]
    assert second["created"] is False
    assert second["reason"] == "lost_creation_race"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Party)) == 1
        # the loser still contributed what it knew: its spelling is now an alias
        party = session.get(Party, first["id"])
        assert party.aliases == []  # "Ltd" is a legal form, so the key is unchanged
