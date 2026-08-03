"""End-to-end pipeline tests against the live stack (LiteLLM, Docling, OpenSearch).

Model output varies between runs, so assertions are structural: stage settlement,
quarantine of genuinely broken files, taxonomy membership, and count lower bounds.
"""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Chunk,
    Document,
    DocumentVersion,
    EvalRecord,
    Matter,
    MatterAssignment,
    ProcessingState,
    Source,
    SourceObject,
)
from knowledge_index.pipeline import PipelineRunner
from knowledge_index.sync import LocalFilesystemSource, SyncEngine
from knowledge_index.config import AppConfig as _AppConfig

pytestmark = pytest.mark.integration

# document typing is ontology-based: every typed node must be visible in scope
ONTOLOGY_NODES = _AppConfig().doc_ontology().visible


def add_source(factory: sessionmaker[Session], root: Path) -> Source:
    with factory() as session:
        source = Source(
            kind="local_fs",
            display_name="mock law firm",
            config={
                "root": str(root),
                "default_acl": [
                    {
                        "principal": "group:ma-team",
                        "principal_kind": "group",
                        "access": "allow",
                    }
                ],
            },
        )
        session.add(source)
        session.commit()
        return source


def sync_source(factory: sessionmaker[Session], source: Source, root: Path) -> None:
    with factory() as session:
        record = session.get(Source, source.id)
        assert record is not None
        SyncEngine(session, record, LocalFilesystemSource(root)).sync()
        session.commit()


def test_pipeline_builds_versioned_knowledge_and_index(
    factory: sessionmaker[Session],
    integration_config: AppConfig,
    settle_pipeline,
    refresh_search,
    tmp_path: Path,
) -> None:
    matter = tmp_path / "Mandate" / "M-2026-0042"
    matter.mkdir(parents=True)
    (matter / "SPA_Entwurf_v1.txt").write_text(
        "Entwurf Unternehmenskaufvertrag. Die Haftung ist unbegrenzt.", encoding="utf-8"
    )
    (matter / "SPA_final.txt").write_text(
        "Finaler Unternehmenskaufvertrag. Die Haftung ist auf den Kaufpreis begrenzt.",
        encoding="utf-8",
    )
    source = add_source(factory, tmp_path)
    sync_source(factory, source, tmp_path)
    config = integration_config

    totals = settle_pipeline(factory, config)
    assert totals["processed"] >= 14  # two files times seven insertion stages, plus real retries
    assert totals["quarantined"] == 0

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Matter)) >= 1
        documents = session.scalars(select(Document)).all()
        assert documents
        for document in documents:
            assert (document.title or "").strip()
            # metadata (the sole typing stage) runs on every version now
            if document.doc_type is not None:
                assert document.doc_type in ONTOLOGY_NODES
        versions = session.scalars(select(DocumentVersion)).all()
        assert len(versions) == 2  # two distinct blobs, deterministic
        assert {version.status for version in versions} == {"draft", "final"}
        final = next(version for version in versions if version.status == "final")
        final_document = session.get(Document, final.document_id)
        assert final_document is not None
        assert final_document.latest_final_version_id == final.id
        assert session.scalar(select(func.count()).select_from(Chunk)) >= 2
        # RL-environment generation is NOT part of insertion any more: no eval yet.
        assert session.scalar(select(func.count()).select_from(EvalRecord)) == 0
        assert not session.scalars(
            select(ProcessingState).where(
                ProcessingState.status.in_(["pending", "running", "failed"])
            )
        ).all()

    refresh_search(config)
    response = httpx.post(
        f"{config.components.opensearch_url}/{config.retrieval.index_name}/_search",
        json={"size": 1, "query": {"match_all": {}}},
        timeout=10,
    )
    response.raise_for_status()
    assert response.json()["hits"]["total"]["value"] >= 2

    # The separate, sparse environment builder proposes candidates from firm work product.
    from knowledge_index.pipeline.environments import EnvironmentBuilder

    build = EnvironmentBuilder(factory, config).build()
    assert build.considered >= 1
    # the judge either proposes it, or explicitly excludes it as external / ineligible
    assert build.proposed + build.skipped_external + build.skipped_ineligible >= 1

    assert PipelineRunner(factory, config).run_until_idle().processed == 0


def test_parallel_files_of_one_matter_converge_on_one_matter(
    factory: sessionmaker[Session],
    integration_config: AppConfig,
    settle_pipeline,
    tmp_path: Path,
) -> None:
    """Three documents of one matter, advanced by three concurrent runners, must end on
    ONE matter: the classify agent creates it with its create_matter tool (committed the
    moment the tool returns), so concurrently classifying siblings find and join it."""
    folder = tmp_path / "Mandate" / "Falke"
    folder.mkdir(parents=True)
    body = (
        "Unser Zeichen: M-2026-0077. Falke GmbH ./. Habicht AG wegen Kaufpreiszahlung "
        "aus dem Unternehmenskaufvertrag vom 12.03.2026. "
    )
    (folder / "Klageschrift.txt").write_text(
        body + "Klageschrift an das Landgericht München I.", encoding="utf-8"
    )
    (folder / "Klageerwiderung.txt").write_text(
        body + "Klageerwiderung der Beklagten.", encoding="utf-8"
    )
    (folder / "Vergleichsvorschlag.txt").write_text(
        body + "Vorschlag zur gütlichen Einigung des Rechtsstreits.", encoding="utf-8"
    )
    source = add_source(factory, tmp_path)
    sync_source(factory, source, tmp_path)
    config = integration_config

    runners = [
        threading.Thread(target=lambda: PipelineRunner(factory, config).run_until_idle())
        for _ in range(3)
    ]
    for thread in runners:
        thread.start()
    for thread in runners:
        thread.join()
    totals = settle_pipeline(factory, config)
    assert totals["quarantined"] == 0

    with factory() as session:
        matters = session.scalars(select(Matter)).all()
        assert len(matters) == 1, [
            (matter.reference_numbers, matter.title) for matter in matters
        ]
        assignments = session.scalars(select(MatterAssignment)).all()
        assert len(assignments) == 3
        assert {assignment.matter_id for assignment in assignments} == {matters[0].id}
        assert "M-2026-0077" in (matters[0].reference_numbers or [])


def test_harvey_corpus_multi_matter_ingestion(
    factory: sessionmaker[Session],
    integration_config: AppConfig,
    settle_pipeline,
    tmp_path: Path,
) -> None:
    """Several matters ingested concurrently from the Harvey corpus: every document
    must land on the matter of its mandate folder, one matter per mandate — no
    duplicates from parallel classification, no cross-matter leakage."""
    corpus = Path(__file__).parent.parent / "testdata" / "harvey" / "mock_dms" / "Mandate"
    mandates = [
        "M-2026-0001 Isda Master Pack",
        "M-2026-0002 Account Control Agreement",
        "M-2026-0003 Repo Securities Lending",
    ]
    per_matter = 4
    for mandate in mandates:
        documents = sorted((corpus / mandate).rglob("*.docx"))
        assert len(documents) >= per_matter
        step = len(documents) // per_matter
        for document in (documents[index * step] for index in range(per_matter)):
            relative = document.relative_to(corpus.parent)
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(document.read_bytes())
    source = add_source(factory, tmp_path)
    sync_source(factory, source, tmp_path)
    config = integration_config

    runners = [
        threading.Thread(target=lambda: PipelineRunner(factory, config).run_until_idle())
        for _ in range(4)
    ]
    for thread in runners:
        thread.start()
    for thread in runners:
        thread.join()
    totals = settle_pipeline(factory, config, timeout_seconds=1800)
    assert totals["quarantined"] == 0

    with factory() as session:
        matters = session.scalars(select(Matter)).all()
        assert len(matters) == len(mandates), [
            (matter.reference_numbers, matter.title) for matter in matters
        ]
        matter_by_ref: dict[str, str] = {}
        for matter in matters:
            for ref in matter.reference_numbers or []:
                matter_by_ref[ref] = matter.id
        assignments = session.scalars(select(MatterAssignment)).all()
        assert len(assignments) == len(mandates) * per_matter
        for assignment in assignments:
            source_object = session.get(SourceObject, assignment.source_object_id)
            expected_ref = source_object.path.split("/")[1].split(" ")[0].upper()
            assert matter_by_ref.get(expected_ref) == assignment.matter_id, (
                source_object.path,
                expected_ref,
                assignment.matter_id,
            )


def test_unsupported_document_is_quarantined_without_blocking_other_files(
    factory: sessionmaker[Session],
    integration_config: AppConfig,
    settle_pipeline,
    tmp_path: Path,
) -> None:
    (tmp_path / "M-2026-0001_ok.txt").write_text("Ein zulässiges Dokument", encoding="utf-8")
    (tmp_path / "M-2026-0001_poison.bin").write_bytes(b"\x00\xff\x00\xff")
    source = add_source(factory, tmp_path)
    sync_source(factory, source, tmp_path)

    totals = settle_pipeline(factory, integration_config)
    assert totals["quarantined"] >= 1
    with factory() as session:
        states = session.scalars(select(ProcessingState)).all()
        poison = [state for state in states if state.status == "quarantined"]
        assert len(poison) == 1
        assert poison[0].stage == "convert"
        assert session.scalar(select(func.count()).select_from(Chunk)) >= 1


def test_environment_builder_is_separate_and_sparse(
    factory: sessionmaker[Session],
    integration_config: AppConfig,
    settle_pipeline,
    tmp_path: Path,
) -> None:
    (tmp_path / "M-2026-0100_NDA_final.txt").write_text(
        "Finale Vertraulichkeitsvereinbarung. Die Parteien wahren Vertraulichkeit.",
        encoding="utf-8",
    )
    source = add_source(factory, tmp_path)
    sync_source(factory, source, tmp_path)
    config = integration_config

    settle_pipeline(factory, config)
    with factory() as session:
        # gen_evals is not a pipeline stage any more: nothing runs inline, nothing exists.
        assert session.scalar(select(func.count()).select_from(EvalRecord)) == 0
        assert (
            session.scalar(
                select(ProcessingState).where(ProcessingState.stage == "gen_evals")
            )
            is None
        )

    from knowledge_index.pipeline.environments import EnvironmentBuilder

    build = EnvironmentBuilder(factory, config).build()
    assert build.considered >= 1
    with factory() as session:
        records = session.scalars(select(EvalRecord)).all()
        for record in records:
            # only firm work product becomes a candidate, and every candidate starts as a
            # proposal — never a live benchmark until a partner approves it
            assert record.authored_internally is True
            assert record.status == "proposed"
            assert record.holdout is True

    # Re-running is idempotent: an already-considered final does not duplicate.
    rebuild = EnvironmentBuilder(factory, config).build()
    assert rebuild.proposed == 0
