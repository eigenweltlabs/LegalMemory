"""Fixture generator checks plus the full generated-estate integration run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    DecisionRecord,
    DocumentVersion,
    DocumentVersionSource,
    EvalRecord,
    ProcessingState,
    Relation,
    Source,
    SourceObject,
)
from knowledge_index.fixtures import generate_mock_dms
from knowledge_index.pipeline.converters import extract_docx_revisions
from knowledge_index.sync import LocalFilesystemSource, SyncEngine
from knowledge_index.verification import verify_fixture


def test_fixture_generator_emits_ground_truth_and_real_ooxml_revisions(
    tmp_path: Path,
) -> None:
    scenario = generate_mock_dms(tmp_path / "fixture", seed=42)
    assert scenario["file_count"] == 15
    manifest = [
        json.loads(line)
        for line in Path(scenario["manifest"]).read_text(encoding="utf-8").splitlines()
    ]
    assert len(manifest) == 15
    assert sum(item["expected_pipeline"] == "quarantined" for item in manifest) == 1
    redline = next(item for item in manifest if "redline" in item["relative_path"])
    converted = extract_docx_revisions(Path(scenario["source_root"]) / redline["relative_path"])
    assert {revision["kind"] for revision in converted.revisions} == {"del", "ins"}
    assert "auf den Kaufpreis begrenzt" in "".join(
        revision["text"] for revision in converted.revisions
    )

    repeated = generate_mock_dms(tmp_path / "fixture-copy", seed=42)
    repeated_manifest = [
        json.loads(line)
        for line in Path(repeated["manifest"]).read_text(encoding="utf-8").splitlines()
    ]
    assert {item["relative_path"]: item["content_hash"] for item in repeated_manifest} == {
        item["relative_path"]: item["content_hash"] for item in manifest
    }

    with pytest.raises(ValueError):
        generate_mock_dms(tmp_path / "fixture", seed=42)


@pytest.mark.integration
def test_generated_fixture_runs_end_to_end_and_passes_the_verifier(
    factory: sessionmaker[Session],
    integration_config: AppConfig,
    settle_pipeline,
    refresh_search,
    tmp_path: Path,
) -> None:
    scenario = generate_mock_dms(tmp_path / "fixture", seed=42)
    source_root = Path(scenario["source_root"])
    acl_map = json.loads(Path(scenario["acl_by_path"]).read_text(encoding="utf-8"))
    with factory() as session:
        source = Source(
            kind="local_fs",
            display_name="mock",
            config={"root": str(source_root), "acl_by_path": acl_map},
        )
        session.add(source)
        session.flush()
        connector = LocalFilesystemSource(
            source_root,
            acl_resolver=lambda path: acl_map[path.relative_to(source_root).as_posix()],
        )
        SyncEngine(session, source, connector).sync()
        session.commit()

    config = integration_config
    config.pipeline.stages["gen_evals"].enabled = True
    totals = settle_pipeline(factory, config)
    assert totals["quarantined"] == 1

    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProcessingState)
                .where(ProcessingState.status == "quarantined")
            )
            == 1
        )
        assert (session.scalar(select(func.count()).select_from(DecisionRecord)) or 0) >= 1
        assert (session.scalar(select(func.count()).select_from(EvalRecord)) or 0) >= 1
        relation_kinds = set(session.scalars(select(Relation.kind)).all())
        assert {
            "supersedes",
            "annex_of",
            "responds_to",
            "belongs_to_thread",
        } <= relation_kinds

        final_hash = next(
            item["content_hash"]
            for item in (
                json.loads(line)
                for line in Path(scenario["manifest"]).read_text(encoding="utf-8").splitlines()
            )
            if item["relative_path"].endswith("Unternehmenskaufvertrag_final.docx")
        )
        version = session.scalar(
            select(DocumentVersion).where(DocumentVersion.content_hash == final_hash)
        )
        assert version is not None, "exact duplicate must resolve to one version"
        assert (
            session.scalar(
                select(func.count())
                .select_from(DocumentVersionSource)
                .where(DocumentVersionSource.version_id == version.id)
            )
            == 2
        )
        source_paths = set(session.scalars(select(SourceObject.path)).all())
        assert len(source_paths) == scenario["file_count"]

    refresh_search(config)
    report = verify_fixture(factory, config, scenario["manifest"])
    assert report["passed"] is True, json.dumps(report, indent=2, ensure_ascii=False)
