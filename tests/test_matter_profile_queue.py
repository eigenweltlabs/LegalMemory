"""What the matter-profile queue promises, stated as tests.

The queue exists so that matter-level facts -- practice group, kind, lifecycle, version
chains -- are re-derived per MATTER rather than per document, because no single document
can decide them. Two properties make that work, and neither is obvious from the table:

1. ten documents landing in one matter at once cost one profile, not ten
2. one document arriving later costs one more profile

The first is what keeps a million-document backfill affordable. The second is what keeps
the answer correct afterwards. They pull in opposite directions, which is why both are
pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.db.models import Matter, MatterProfileQueue
from knowledge_index.pipeline.matter_profile import (
    DEFAULT_DEBOUNCE_SECONDS,
    due_matters,
    mark_matter_dirty,
)


def _add_matter(session: Session, matter_id: str) -> None:
    session.add(
        Matter(
            id=matter_id,
            reference_numbers=[f"M-2026-{matter_id}"],
            title=f"Matter {matter_id}",
        )
    )
    session.flush()


def test_ten_documents_at_once_cost_one_profile(factory: sessionmaker[Session]) -> None:
    """A flood collapses onto one row, and stays undue until the flood stops."""
    now = datetime.now(UTC)
    with factory() as session:
        _add_matter(session, "flood")
        for offset in range(10):
            mark_matter_dirty(session, "flood", now=now + timedelta(seconds=offset))
        session.commit()

        # One row, not ten: the upsert is what turns a corpus into one profile per matter.
        rows = session.scalars(select(MatterProfileQueue)).all()
        assert [row.matter_id for row in rows] == ["flood"]

        # Still landing, so not yet due. Each mark pushes last_marked_at forward, so the
        # debounce window restarts -- without this the first document could go due while
        # the other nine were still arriving, and the matter would profile twice.
        assert due_matters(session, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS) == []

        # The flood ends and the window passes: exactly one profile is owed.
        row = session.get(MatterProfileQueue, "flood")
        row.last_marked_at = now - timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 60)
        session.commit()
        assert due_matters(session, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS) == ["flood"]


def test_a_document_arriving_later_owes_another_profile(
    factory: sessionmaker[Session],
) -> None:
    """Profiling dequeues the matter; the next document queues it again."""
    settled = datetime.now(UTC) - timedelta(seconds=DEFAULT_DEBOUNCE_SECONDS + 60)
    with factory() as session:
        _add_matter(session, "later")
        mark_matter_dirty(session, "later", now=settled)
        session.commit()
        assert due_matters(session, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS) == ["later"]

        # profile_matter deletes the row on success, which is what makes the matter clean
        # rather than permanently due.
        session.delete(session.get(MatterProfileQueue, "later"))
        session.commit()
        assert due_matters(session, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS) == []

        # A document arrives a week after the matter was profiled. The facts it carries
        # may change the matter's own facts, so the matter is owed a fresh profile --
        # re-running is safe because profiling is idempotent.
        mark_matter_dirty(session, "later", now=settled)
        session.commit()
        assert due_matters(session, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS) == ["later"]


def test_the_sweep_takes_everything_queued_regardless_of_the_window(
    factory: sessionmaker[Session],
) -> None:
    """``ignore_debounce`` is what makes the window a cost knob, not a correctness one.

    A matter that never goes quiet for a full window would otherwise never be profiled.
    """
    with factory() as session:
        _add_matter(session, "busy")
        mark_matter_dirty(session, "busy", now=datetime.now(UTC))
        session.commit()

        assert due_matters(session, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS) == []
        assert due_matters(
            session, debounce_seconds=DEFAULT_DEBOUNCE_SECONDS, ignore_debounce=True
        ) == ["busy"]


def test_the_client_is_replaced_not_accumulated(factory: sessionmaker[Session]) -> None:
    """One matter, one client — whatever the documents happened to call "the client".

    The per-document extractor linked any party a document called the client, so a
    target, a fund under review and a counterparty each became one: 222 clients and
    150 multi-client matters on a corpus whose firm has 46.
    """
    from knowledge_index.db.models import Client, MatterClient
    from knowledge_index.pipeline.matter_profile import MatterProfile, apply_client

    with factory() as session:
        _add_matter(session, "client-test")
        matter = session.get(Matter, "client-test")
        # what the documents accrued: the target and a fund, neither of them the client
        for name in ("Cascadia Renewables Holdings, Inc.", "Meridian Peak Fund IV, L.P."):
            c = Client(name=name)
            session.add(c)
            session.flush()
            session.add(MatterClient(matter_id=matter.id, client_id=c.id))
        session.commit()
        assert session.query(MatterClient).filter_by(matter_id=matter.id).count() == 2

        profile = MatterProfile(
            lifecycle="executed",
            summary="x",
            evidence="y",
            client_name="Ironside Capital Management LP",
            client_evidence="engagement-letter.docx: 'we act for Ironside'",
        )
        assert apply_client(session, matter, profile) == "Ironside Capital Management LP"
        session.commit()

        rows = session.query(MatterClient).filter_by(matter_id=matter.id).all()
        assert len(rows) == 1
        assert session.get(Client, rows[0].client_id).name == "Ironside Capital Management LP"


def test_a_client_already_known_is_reused_not_duplicated(
    factory: sessionmaker[Session],
) -> None:
    """The same client across forty matters is one row, keyed on the normalised name."""
    from knowledge_index.db.models import Client, MatterClient
    from knowledge_index.pipeline.matter_profile import MatterProfile, apply_client

    with factory() as session:
        existing = Client(name="Ironside Capital Management LP")
        session.add(existing)
        _add_matter(session, "reuse-test")
        session.commit()

        matter = session.get(Matter, "reuse-test")
        apply_client(
            session,
            matter,
            MatterProfile(lifecycle="executed", summary="x", evidence="y",
                          client_name="Ironside Capital Management, L.P."),
        )
        session.commit()

        assert session.query(Client).count() == 1
        row = session.query(MatterClient).filter_by(matter_id=matter.id).one()
        assert row.client_id == existing.id
