"""Index-stage double-execution guard (2026-08-01 run audit): a version fed by
two source files (a duplicate merge) gets one index task per source — running
both must yield the corpus once, and the database itself refuses twin chunks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import knowledge_index.pipeline.runner as runner_module
import knowledge_index.search_backend as search_backend_module
from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    Blob,
    Chunk,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    ProcessingState,
    Source,
    SourceObject,
)
from knowledge_index.pipeline.runner import PipelineRunner


class _FakeIndex:
    """Stands in for OpenSearchIndex: records the sync calls, touches nothing."""

    calls: list[tuple[list[str], int]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def bulk_sync(self, *, deletes, upserts):
        _FakeIndex.calls.append(([*deletes], len(upserts)))


def _seed_twin_sources(factory: sessionmaker) -> None:
    # One document version fed by TWO source objects — the shape a duplicate
    # merge produces — each with its own index processing_state.
    with factory() as session:
        session.add(Source(id="src-1", kind="local_fs", display_name="s", config={}))
        for n in (1, 2):
            session.add(Blob(content_hash=f"h{n}", size_bytes=8))
        session.flush()
        for n in (1, 2):
            session.add(
                Artifact(
                    content_hash=f"h{n}",
                    producer="p",
                    producer_version="v1",
                    kind="structured_json",
                    payload={"text": "Identical agreement text. " * 40},
                )
            )
        session.flush()
        for n in (1, 2):
            session.add(
                SourceObject(
                    id=f"so-{n}",
                    source_id="src-1",
                    external_id=f"ext-{n}",
                    path=f"/twin-{n}/file.docx",
                    name="file.docx",
                    content_hash=f"h{n}",
                )
            )
        session.add(Document(id="doc-1", title="Agreement", matter_id=None))
        session.flush()
        session.add(
            DocumentVersion(
                id="ver-1", document_id="doc-1", ordinal=1, status="final", content_hash="h1"
            )
        )
        session.flush()
        for n in (1, 2):
            session.add(DocumentVersionSource(version_id="ver-1", source_object_id=f"so-{n}"))
        session.commit()


def _run_index(factory: sessionmaker, tmp_path: Path, source_object_id: str) -> None:
    runner = PipelineRunner(factory, AppConfig(artifact_dir=tmp_path))
    with factory() as session:
        state = ProcessingState(source_object_id=source_object_id, stage="index")
        runner._index(session, state)
        session.commit()


def _chunk_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_version_id == "ver-1")
    )


def test_two_index_tasks_on_one_version_store_the_corpus_once(
    factory: sessionmaker, tmp_path: Path, monkeypatch
) -> None:
    _seed_twin_sources(factory)
    monkeypatch.setattr(runner_module, "embed_text", lambda *a, **k: [0.0, 1.0])
    monkeypatch.setattr(search_backend_module, "OpenSearchIndex", _FakeIndex)
    _FakeIndex.calls = []

    _run_index(factory, tmp_path, "so-1")
    with factory() as session:
        first_count = _chunk_count(session)
    assert first_count > 0

    # The twin's own index task runs afterwards — same version, no new chunks.
    _run_index(factory, tmp_path, "so-2")
    with factory() as session:
        assert _chunk_count(session) == first_count
        # every ordinal exists exactly once
        assert (
            session.scalar(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.document_version_id == "ver-1")
                .group_by(Chunk.ordinal)
                .having(func.count() > 1)
            )
            is None
        )


def test_database_refuses_twin_chunks(factory: sessionmaker) -> None:
    _seed_twin_sources(factory)
    with factory() as session:
        session.add(Chunk(document_version_id="ver-1", ordinal=7, text="a"))
        session.commit()
    with factory() as session:
        session.add(Chunk(document_version_id="ver-1", ordinal=7, text="b"))
        with pytest.raises(IntegrityError):
            session.commit()
