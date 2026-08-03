"""extract_metadata doc_date fallback: mtime is only a document date when the
connector marks it trustworthy. For managed imports (local_fs copies files into
appdata) mtime is the ingestion day, so it must NOT become doc_date — the
document stays undated instead of confidently wrong. See
docs/ontology-v1-improvement-spec.md O10 and the audit §4.1.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    ProcessingState,
    Source,
    SourceObject,
)
from knowledge_index.pipeline import runner as runner_module
from knowledge_index.pipeline.extraction import DocumentMetadata
from knowledge_index.pipeline.runner import PipelineRunner

MTIME = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)  # the "ingestion day" for a managed import


def _seed(factory: sessionmaker, *, trust_mtime: bool) -> None:
    with factory() as session:
        session.add(Blob(content_hash="h1", size_bytes=8))
        session.flush()  # artifacts.content_hash → blobs.content_hash
        session.add(
            Source(
                id="src-1",
                kind="local_fs",
                display_name="Test source",
                config={"trust_mtime": trust_mtime},
            )
        )
        session.add(
            Artifact(
                content_hash="h1",
                producer="p",
                producer_version="v1",
                kind="structured_json",
                payload={"text": "Ein Vertrag ohne Datum im Text."},
            )
        )
        session.flush()
        session.add(
            SourceObject(
                id="so-1",
                source_id="src-1",
                external_id="ext-1",
                path="/x/file.txt",
                name="file.txt",
                content_hash="h1",
                mtime=MTIME,
            )
        )
        document = Document(id="doc-1", title="Vertrag", doc_type=None, matter_id=None)
        session.add(document)
        session.flush()
        session.add(
            DocumentVersion(
                id="ver-1", document_id="doc-1", ordinal=1, status="final", content_hash="h1"
            )
        )
        session.flush()
        session.add(DocumentVersionSource(version_id="ver-1", source_object_id="so-1"))
        session.commit()


def _stub_metadata() -> DocumentMetadata:
    # doc_date=None → the model found no date in the content, so the fallback decides.
    # type_node=None → "no ontology type fits", the honest untyped branch; keeps this
    # test focused on the doc_date logic without needing a resolved ontology node.
    return DocumentMetadata(
        type_node=None,
        language="de",
        doc_date=None,
        title="Vertrag",
        confidence=0.9,
    )


def _run(factory: sessionmaker, tmp_path: Path, monkeypatch) -> Document:
    # extract_metadata is agentic (chat_agent); patch it to return the stub directly.
    monkeypatch.setattr(runner_module, "chat_agent", lambda *a, **k: _stub_metadata())
    runner = PipelineRunner(factory, AppConfig(artifact_dir=tmp_path))
    with factory() as session:
        state = ProcessingState(source_object_id="so-1", stage="extract_metadata")
        runner._extract_metadata(session, state)
        session.commit()
    with factory() as verify:
        return verify.get(Document, "doc-1")


def test_untrusted_mtime_leaves_doc_date_null(
    factory: sessionmaker, tmp_path: Path, monkeypatch
) -> None:
    _seed(factory, trust_mtime=False)
    document = _run(factory, tmp_path, monkeypatch)
    assert document.doc_date is None
    assert document.provenance["doc_date_source"] == "none"


def test_trusted_mtime_is_used_as_fallback(
    factory: sessionmaker, tmp_path: Path, monkeypatch
) -> None:
    _seed(factory, trust_mtime=True)
    document = _run(factory, tmp_path, monkeypatch)
    assert document.doc_date == MTIME
    assert document.provenance["doc_date_source"] == "file_mtime"


def test_matter_time_range_aggregates_content_dates_only(factory: sessionmaker) -> None:
    """The matter's span is min/max of its documents' CONTENT dates: mtime-derived
    dates are excluded (a storage timestamp is not matter activity), imported
    matters stay untouched (practice management is authoritative), and a matter
    with no content-dated documents keeps an honest NULL span."""
    with factory() as session:
        matter = Matter(title="M", reference_numbers=["M-1"], imported=False)
        imported = Matter(
            title="I",
            reference_numbers=["I-1"],
            imported=True,
            time_range={"from": "2001-01-01T00:00:00", "to": "2002-01-01T00:00:00"},
        )
        session.add_all([matter, imported])
        session.flush()
        session.add_all(
            [
                Document(
                    id="d1",
                    matter_id=matter.id,
                    title="early",
                    doc_date=datetime(2025, 3, 1, tzinfo=UTC),
                    provenance={"doc_date_source": "document_content"},
                ),
                Document(
                    id="d2",
                    matter_id=matter.id,
                    title="late",
                    doc_date=datetime(2025, 6, 15, tzinfo=UTC),
                    provenance={"doc_date_source": "document_content"},
                ),
                Document(
                    id="d3",
                    matter_id=matter.id,
                    title="mtime-derived, must not count",
                    doc_date=datetime(2026, 7, 23, tzinfo=UTC),
                    provenance={"doc_date_source": "file_mtime"},
                ),
                Document(
                    id="d4",
                    matter_id=imported.id,
                    title="imported matter member",
                    doc_date=datetime(2025, 1, 1, tzinfo=UTC),
                    provenance={"doc_date_source": "document_content"},
                ),
            ]
        )
        session.flush()

        runner_module._refresh_matter_time_range(session, matter)
        runner_module._refresh_matter_time_range(session, imported)
        assert matter.time_range == {
            "from": datetime(2025, 3, 1, tzinfo=UTC).isoformat(),
            "to": datetime(2025, 6, 15, tzinfo=UTC).isoformat(),
        }
        # authoritative import untouched
        assert imported.time_range == {
            "from": "2001-01-01T00:00:00",
            "to": "2002-01-01T00:00:00",
        }

        # all content dates gone -> span honestly NULL again
        for document_id in ("d1", "d2"):
            session.get(Document, document_id).provenance = {"doc_date_source": "file_mtime"}
        session.flush()
        runner_module._refresh_matter_time_range(session, matter)
        assert matter.time_range is None


def test_extract_metadata_refreshes_the_matters_span(
    factory: sessionmaker, tmp_path: Path, monkeypatch
) -> None:
    """The stage that writes a document's date also re-derives its matter's span."""
    _seed(factory, trust_mtime=False)
    with factory() as session:
        matter = Matter(title="M", reference_numbers=["M-1"], imported=False)
        session.add(matter)
        session.flush()
        matter_id = matter.id
        session.get(Document, "doc-1").matter_id = matter_id
        session.commit()

    dated = DocumentMetadata(
        type_node=None,
        language="de",
        doc_date="2025-04-01",
        title="Vertrag",
        confidence=0.9,
    )
    monkeypatch.setattr(runner_module, "chat_agent", lambda *a, **k: dated)
    runner = PipelineRunner(factory, AppConfig(artifact_dir=tmp_path))
    with factory() as session:
        state = ProcessingState(source_object_id="so-1", stage="extract_metadata")
        runner._extract_metadata(session, state)
        session.commit()
    with factory() as verify:
        refreshed = verify.get(Matter, matter_id)
        assert refreshed.time_range is not None
        assert refreshed.time_range["from"].startswith("2025-04-01")
        assert refreshed.time_range["to"].startswith("2025-04-01")
