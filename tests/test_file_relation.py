from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig, PipelineConfig
from knowledge_index.db.models import (
    Artifact,
    Blob,
    CommunicationThread,
    DocumentVersionSource,
    Matter,
    MatterAssignment,
    ProcessingState,
    Relation,
    RelationIntent,
    Source,
    SourceObject,
)
from knowledge_index.pipeline.extraction import FileRelationResult
from knowledge_index.pipeline.folder_context import (
    folder_ls,
    linked_document,
    linked_version,
)
from knowledge_index.pipeline.matter_search import open_source_file
from knowledge_index.pipeline.runner import PipelineRunner, StageResult


def _converted_object(
    session: Session, source: Source, path: str, content_hash: str, text: str
) -> SourceObject:
    source_object = SourceObject(
        source_id=source.id,
        external_id=path,
        path=path,
        name=path.rsplit("/", 1)[-1],
        content_hash=content_hash,
    )
    session.add(Blob(content_hash=content_hash, size_bytes=len(text)))
    session.flush()
    session.add(source_object)
    session.add(
        Artifact(
            content_hash=content_hash,
            producer="test-converter",
            producer_version="1",
            kind="structured_json",
            payload={"text": text},
        )
    )
    session.flush()
    return source_object


def test_relation_context_lists_every_file_and_opens_exact_path(session: Session) -> None:
    source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
    session.add(source)
    session.flush()
    current = _converted_object(
        session,
        source,
        "Clients/A/M-1/Contracts/current.txt",
        "a" * 64,
        "current-file-content",
    )
    for index in range(105):
        _converted_object(
            session,
            source,
            f"Clients/A/M-1/Contracts/candidate-{index:03}.txt",
            f"{index + 2:064x}",
            f"candidate {index}",
        )

    listing = folder_ls(
        session,
        source.id,
        current.path,
        up=1,
        down=1,
        per_folder_limit=None,
        max_folders=None,
    )
    assert "candidate-000.txt" in listing
    assert "candidate-104.txt" in listing
    assert "(+" not in listing

    opened = open_source_file(session, source.id, current.path)
    assert opened["ref"] == current.id
    assert opened["path"] == current.path
    assert opened["text"] == "current-file-content"


def test_file_relation_materializes_only_current_file_and_target_edge(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        current = _converted_object(
            session, source, "M-1/current.txt", "c" * 64, "Current contract annex"
        )
        target = _converted_object(
            session, source, "M-1/main.txt", "d" * 64, "Main contract"
        )
        session.add_all(
            [
                MatterAssignment(
                    source_object_id=current.id,
                    matter_id=matter.id,
                    confidence=1,
                    producer_version="test",
                ),
                MatterAssignment(
                    source_object_id=target.id,
                    matter_id=matter.id,
                    confidence=1,
                    producer_version="test",
                ),
            ]
        )
        session.flush()

        result = FileRelationResult.model_validate(
            {
                "logical_title": "Annex A",
                "doc_type": "other_annex",
                "status": "final",
                "identity": "new_document",
                "relations": [
                    {
                        "target_ref": target.id,
                        "direction": "current_to_target",
                        "kind": "annex_of",
                        "evidence": "The current file identifies itself as Annex A.",
                    }
                ],
                "confidence": 0.9,
            }
        )
        PipelineRunner(factory, AppConfig())._materialize_file_relation(
            session, matter, current, result, "mvp-6"
        )
        session.commit()

        assert (
            session.scalar(select(func.count()).select_from(DocumentVersionSource)) == 2
        )
        edge = session.scalar(select(Relation).where(Relation.kind == "annex_of"))
        assert edge is not None
        assert edge.from_id != edge.to_id


def test_unclassified_target_parks_intents_and_classify_replays(
    factory: sessionmaker[Session],
) -> None:
    """A relate decision against a readable but unclassified target is parked, not
    dropped, and lands the moment the target's matter assignment arrives."""
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        current = _converted_object(
            session, source, "M-1/reply.eml", "e" * 64, "Reply quoting the original"
        )
        target = _converted_object(
            session, source, "M-1/original.eml", "f" * 64, "Original email"
        )
        session.add(
            MatterAssignment(
                source_object_id=current.id,
                matter_id=matter.id,
                confidence=1,
                producer_version="test",
            )
        )
        session.flush()

        result = FileRelationResult.model_validate(
            {
                "logical_title": "Reply",
                "status": "final",
                "identity": "new_document",
                "redline_of": target.id,
                "relations": [
                    {
                        "target_ref": target.id,
                        "direction": "current_to_target",
                        "kind": "responds_to",
                        "evidence": "The reply quotes the original email.",
                    }
                ],
                "thread_subject": "Deal Alpha",
                "thread_member_refs": [target.id],
                "confidence": 0.9,
            }
        )
        runner = PipelineRunner(factory, AppConfig())
        runner._materialize_file_relation(session, matter, current, result, "mvp-6")
        session.commit()

        intents = session.scalars(select(RelationIntent)).all()
        assert {intent.intent for intent in intents} == {
            "relation",
            "thread_member",
            "redline",
        }
        assert all(intent.status == "pending" for intent in intents)
        assert (
            session.scalar(select(Relation).where(Relation.kind == "responds_to")) is None
        )

        # the target classifies later (into its own matter) -> replay applies everything
        other_matter = Matter(title="M-2", reference_numbers=["M-2"], imported=False)
        session.add(other_matter)
        session.flush()
        session.add(
            MatterAssignment(
                source_object_id=target.id,
                matter_id=other_matter.id,
                confidence=1,
                producer_version="test",
            )
        )
        session.flush()
        assert runner._apply_relation_intents(session, target.id) == 3
        session.commit()

        current_document = linked_document(session, current.id)
        target_document = linked_document(session, target.id)
        edge = session.scalar(select(Relation).where(Relation.kind == "responds_to"))
        assert edge is not None
        assert edge.from_id == current_document.id
        assert edge.to_id == target_document.id
        assert linked_version(session, current.id).redline_against == (
            linked_version(session, target.id).id
        )
        thread = session.scalar(
            select(CommunicationThread).where(
                CommunicationThread.subject_norm == "Deal Alpha"
            )
        )
        assert thread is not None and thread.matter_id == matter.id
        membership = session.scalar(
            select(Relation).where(
                Relation.kind == "belongs_to_thread",
                Relation.from_id == target_document.id,
                Relation.to_id == thread.id,
            )
        )
        assert membership is not None
        # replay is idempotent: nothing pending remains
        assert runner._apply_relation_intents(session, target.id) == 0


def test_rematerialize_clears_stale_pending_intents(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        current = _converted_object(session, source, "M-1/a.txt", "1" * 64, "a")
        target = _converted_object(session, source, "M-1/b.txt", "2" * 64, "b")
        session.add(
            MatterAssignment(
                source_object_id=current.id,
                matter_id=matter.id,
                confidence=1,
                producer_version="test",
            )
        )
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        with_relation = FileRelationResult.model_validate(
            {
                "logical_title": "A",
                "status": "final",
                "identity": "new_document",
                "relations": [
                    {
                        "target_ref": target.id,
                        "direction": "current_to_target",
                        "kind": "references",
                        "evidence": "cites b",
                    }
                ],
                "confidence": 0.9,
            }
        )
        runner._materialize_file_relation(session, matter, current, with_relation, "mvp-6")
        session.commit()
        assert session.scalar(select(func.count()).select_from(RelationIntent)) == 1

        # a re-relate that no longer claims the relation supersedes the parked decision
        without_relation = FileRelationResult.model_validate(
            {
                "logical_title": "A",
                "status": "final",
                "identity": "new_document",
                "confidence": 0.9,
            }
        )
        runner._materialize_file_relation(
            session, matter, current, without_relation, "mvp-6"
        )
        session.commit()
        assert session.scalar(select(func.count()).select_from(RelationIntent)) == 0


def _stage_state(
    session: Session, source_object_id: str, stage: str, status: str
) -> ProcessingState:
    state = ProcessingState(
        source_object_id=source_object_id, stage=stage, status=status
    )
    session.add(state)
    return state


def test_ensure_source_object_ready_runs_stages_through_claim_machinery(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.flush()
        content_hash = "9" * 64
        session.add(Blob(content_hash=content_hash, size_bytes=1))
        target = SourceObject(
            source_id=source.id,
            external_id="M-1/late.txt",
            path="M-1/late.txt",
            name="late.txt",
            content_hash=content_hash,
        )
        session.add(target)
        session.flush()
        _stage_state(session, target.id, "fetch", "done")
        _stage_state(session, target.id, "convert", "pending")
        session.commit()
        target_id = target.id

    runner = PipelineRunner(factory, AppConfig())

    def fake_convert(session: Session, state: ProcessingState):
        source_object = session.get(SourceObject, state.source_object_id)
        session.add(
            Artifact(
                content_hash=source_object.content_hash,
                producer="test-converter",
                producer_version="1",
                kind="structured_json",
                payload={"text": "converted inline"},
            )
        )
        return StageResult()

    runner._convert = fake_convert
    assert runner.ensure_source_object_ready(target_id) == {"status": "ready"}
    with factory() as session:
        state = session.scalar(
            select(ProcessingState).where(
                ProcessingState.source_object_id == target_id,
                ProcessingState.stage == "convert",
            )
        )
        assert state.status == "done"
        assert state.attempts == 1
        assert state.producer_version is not None
        opened = open_source_file(session, source.id, "M-1/late.txt")
        assert opened["text"] == "converted inline"


def test_ensure_source_object_ready_reports_quarantine_and_running(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.flush()
        quarantined = SourceObject(
            source_id=source.id,
            external_id="M-1/broken.bin",
            path="M-1/broken.bin",
            name="broken.bin",
            content_hash="8" * 64,
        )
        running = SourceObject(
            source_id=source.id,
            external_id="M-1/busy.txt",
            path="M-1/busy.txt",
            name="busy.txt",
            content_hash="7" * 64,
        )
        session.add_all(
            [
                Blob(content_hash="8" * 64, size_bytes=1),
                Blob(content_hash="7" * 64, size_bytes=1),
                quarantined,
                running,
            ]
        )
        session.flush()
        _stage_state(session, quarantined.id, "fetch", "done")
        _stage_state(session, quarantined.id, "convert", "quarantined")
        _stage_state(session, running.id, "fetch", "done")
        running_convert = _stage_state(session, running.id, "convert", "running")
        running_convert.claimed_at = datetime.now(UTC)
        session.commit()
        quarantined_id, running_id = quarantined.id, running.id

    config = AppConfig(pipeline=PipelineConfig(inline_conversion_budget_seconds=1))
    runner = PipelineRunner(factory, config)
    assert runner.ensure_source_object_ready(quarantined_id) == {"status": "quarantined"}
    # a claim held by another worker is observed, never duplicated
    assert runner.ensure_source_object_ready(running_id) == {"status": "in_progress"}


def test_open_source_file_pulls_conversion_on_request(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.flush()
        content_hash = "6" * 64
        session.add(Blob(content_hash=content_hash, size_bytes=1))
        target = SourceObject(
            source_id=source.id,
            external_id="M-1/fresh.txt",
            path="M-1/fresh.txt",
            name="fresh.txt",
            content_hash=content_hash,
        )
        session.add(target)
        session.commit()
        source_id, target_id = source.id, target.id

    calls: list[str] = []

    def convert_now(ref: str) -> dict:
        calls.append(ref)
        with factory() as inner:
            inner.add(
                Artifact(
                    content_hash=content_hash,
                    producer="test-converter",
                    producer_version="1",
                    kind="structured_json",
                    payload={"text": "converted on request"},
                )
            )
            inner.commit()
        return {"status": "ready"}

    with factory() as session:
        opened = open_source_file(
            session, source_id, "M-1/fresh.txt", ensure_ready=convert_now
        )
    assert calls == [target_id]
    assert opened["text"] == "converted on request"

    def never_ready(ref: str) -> dict:
        return {"status": "in_progress"}

    with factory() as session:
        session.execute(
            Artifact.__table__.delete().where(Artifact.content_hash == content_hash)
        )
        session.commit()
        blocked = open_source_file(
            session, source_id, "M-1/fresh.txt", ensure_ready=never_ready
        )
    assert "not readable yet" in blocked["error"]
    assert "in_progress" in blocked["error"]


def _chain_document(session: Session, matter: Matter, ordinals: list[int]):
    """A document with one version per ordinal, for order-insertion tests."""
    from knowledge_index.db.models import Document, DocumentVersion

    document = Document(matter_id=matter.id, title="Chain")
    session.add(document)
    session.flush()
    versions = []
    for ordinal in ordinals:
        version = DocumentVersion(
            document_id=document.id, content_hash="c" * 64, ordinal=ordinal
        )
        session.add(version)
        versions.append(version)
    session.flush()
    return document, versions


def _supersedes_pairs(session: Session) -> set[tuple[str, str]]:
    return {
        (relation.from_id, relation.to_id)
        for relation in session.scalars(
            select(Relation).where(Relation.kind == "supersedes")
        ).all()
    }


def test_after_inserts_adjacent_to_anchor_not_at_chain_end(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        matter = Matter(title="M", reference_numbers=["M"], imported=False)
        session.add(Blob(content_hash="c" * 64, size_bytes=1))
        session.add(matter)
        session.flush()
        document, (v1, v2, v3) = _chain_document(session, matter, [1, 2, 3])
        from knowledge_index.db.models import DocumentVersion

        new = DocumentVersion(document_id=document.id, content_hash="c" * 64)
        session.add(new)
        session.flush()

        runner = PipelineRunner(factory, AppConfig())
        runner._order_file_version(session, document, new, v1, "after", {})
        session.commit()

        assert new.ordinal == 2  # directly after the anchor, not max+1
        assert (v1.ordinal, v2.ordinal, v3.ordinal) == (1, 3, 4)
        assert _supersedes_pairs(session) == {(new.id, v1.id)}


def test_unknown_order_stays_unknown_and_cannot_crown_latest_final(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        anchor = _converted_object(session, source, "M-1/v1.txt", "a" * 64, "version one")
        late = _converted_object(session, source, "M-1/vx.txt", "b" * 64, "version x")
        for obj in (anchor, late):
            session.add(
                MatterAssignment(
                    source_object_id=obj.id,
                    matter_id=matter.id,
                    confidence=1,
                    producer_version="test",
                )
            )
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        runner._materialize_file_relation(
            session,
            matter,
            anchor,
            FileRelationResult.model_validate(
                {
                    "logical_title": "Agreement",
                    "status": "final",
                    "identity": "new_document",
                    "confidence": 0.9,
                }
            ),
            "mvp-7",
        )
        runner._materialize_file_relation(
            session,
            matter,
            late,
            FileRelationResult.model_validate(
                {
                    "logical_title": "Agreement",
                    "status": "final",
                    "identity": "new_version",
                    "same_document_ref": anchor.id,
                    "relative_order": "unknown",
                    "confidence": 0.9,
                }
            ),
            "mvp-7",
        )
        session.commit()

        anchor_version = linked_version(session, anchor.id)
        late_version = linked_version(session, late.id)
        assert late_version.document_id == anchor_version.document_id
        assert late_version.ordinal is None  # unknown stays unknown
        assert _supersedes_pairs(session) == set()  # no fabricated supersession
        document = linked_document(session, anchor.id)
        # the known-position final keeps the crown; the unknown one cannot take it
        assert document.latest_final_version_id == anchor_version.id


def test_same_order_shares_ordinal_without_supersedes(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        matter = Matter(title="M", reference_numbers=["M"], imported=False)
        session.add(Blob(content_hash="c" * 64, size_bytes=1))
        session.add(matter)
        session.flush()
        document, (v1, v2) = _chain_document(session, matter, [1, 2])
        from knowledge_index.db.models import DocumentVersion

        new = DocumentVersion(document_id=document.id, content_hash="c" * 64)
        session.add(new)
        session.flush()

        runner = PipelineRunner(factory, AppConfig())
        runner._order_file_version(session, document, new, v2, "same", {})
        session.commit()

        assert new.ordinal == 2
        assert (v1.ordinal, v2.ordinal) == (1, 2)
        assert _supersedes_pairs(session) == set()


def test_rerun_replaces_stale_supersedes_instead_of_accreting(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        matter = Matter(title="M", reference_numbers=["M"], imported=False)
        session.add(Blob(content_hash="c" * 64, size_bytes=1))
        session.add(matter)
        session.flush()
        document, (anchor,) = _chain_document(session, matter, [1])
        from knowledge_index.db.models import DocumentVersion

        new = DocumentVersion(document_id=document.id, content_hash="c" * 64)
        session.add(new)
        session.flush()
        runner = PipelineRunner(factory, AppConfig())

        runner._order_file_version(session, document, new, anchor, "after", {})
        session.commit()
        assert _supersedes_pairs(session) == {(new.id, anchor.id)}

        # the model changes its mind on a re-run: order flips
        runner._order_file_version(session, document, new, anchor, "before", {})
        session.commit()
        assert _supersedes_pairs(session) == {(anchor.id, new.id)}

        # and finally admits it does not know: every claim between the pair goes
        runner._order_file_version(session, document, new, anchor, "unknown", {})
        session.commit()
        assert _supersedes_pairs(session) == set()
        assert new.ordinal is None


def _materialized(session: Session, runner, matter, obj, payload: dict) -> None:
    runner._materialize_file_relation(
        session, matter, obj, FileRelationResult.model_validate(payload), "mvp-7"
    )


def _assigned_object(session, source, matter, path, content_hash, text):
    obj = _converted_object(session, source, path, content_hash, text)
    session.add(
        MatterAssignment(
            source_object_id=obj.id,
            matter_id=matter.id,
            confidence=1,
            producer_version="test",
        )
    )
    session.flush()
    return obj


def test_duplicate_and_new_version_merges_leave_no_husks(
    factory: sessionmaker[Session],
) -> None:
    from knowledge_index.db.models import Document, DocumentVersion

    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        base = _assigned_object(session, source, matter, "M-1/base.txt", "a" * 64, "v1")
        copy = _assigned_object(session, source, matter, "M-1/copy.txt", "b" * 64, "v1")
        newer = _assigned_object(session, source, matter, "M-1/v2.txt", "d" * 64, "v2")

        _materialized(
            session, runner, matter, base,
            {"logical_title": "Agreement", "status": "draft",
             "identity": "new_document", "confidence": 0.9},
        )
        _materialized(
            session, runner, matter, copy,
            {"logical_title": "Agreement", "status": "draft", "identity": "duplicate",
             "duplicate_of": base.id, "confidence": 0.9},
        )
        _materialized(
            session, runner, matter, newer,
            {"logical_title": "Agreement", "status": "final", "identity": "new_version",
             "same_document_ref": base.id, "relative_order": "after",
             "confidence": 0.9},
        )
        session.commit()

        # one logical document, two versions (base+copy share one), zero husks
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        versions = session.scalars(select(DocumentVersion)).all()
        assert len(versions) == 2
        base_version = linked_version(session, base.id)
        assert linked_version(session, copy.id).id == base_version.id


def test_dangling_edge_to_provisional_document_is_repointed_on_merge(
    factory: sessionmaker[Session],
) -> None:
    from knowledge_index.db.models import Document

    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        existing = _assigned_object(session, source, matter, "M-1/x.txt", "1" * 64, "x")
        pending = _assigned_object(session, source, matter, "M-1/b.txt", "2" * 64, "x")
        referrer = _assigned_object(session, source, matter, "M-1/a.txt", "3" * 64, "a")

        _materialized(
            session, runner, matter, existing,
            {"logical_title": "X", "status": "final",
             "identity": "new_document", "confidence": 0.9},
        )
        # referrer links to `pending` BEFORE pending's own relate ran -> the edge
        # lands on pending's provisional document
        _materialized(
            session, runner, matter, referrer,
            {"logical_title": "A", "status": "final", "identity": "new_document",
             "relations": [{"target_ref": pending.id,
                            "direction": "current_to_target",
                            "kind": "references", "evidence": "cites b"}],
             "redline_of": pending.id,
             "confidence": 0.9},
        )
        provisional_document = linked_document(session, pending.id)
        edge = session.scalar(select(Relation).where(Relation.kind == "references"))
        assert edge.to_id == provisional_document.id

        # pending's own relate says: I'm a duplicate of `existing` -> provisional
        # merges away; the edge and the redline pointer must follow the survivor
        _materialized(
            session, runner, matter, pending,
            {"logical_title": "X", "status": "final", "identity": "duplicate",
             "duplicate_of": existing.id, "confidence": 0.9},
        )
        session.commit()

        surviving_document = linked_document(session, existing.id)
        edge = session.scalar(select(Relation).where(Relation.kind == "references"))
        assert edge.to_id == surviving_document.id
        assert linked_version(session, referrer.id).redline_against == (
            linked_version(session, existing.id).id
        )
        assert session.get(Document, provisional_document.id) is None  # husk gone


def test_identity_flip_on_rerun_removes_selfloop_supersedes(
    factory: sessionmaker[Session],
) -> None:
    from knowledge_index.db.models import Document, DocumentVersion

    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        anchor = _assigned_object(session, source, matter, "M-1/t.txt", "5" * 64, "t")
        flip = _assigned_object(session, source, matter, "M-1/c.txt", "6" * 64, "t")

        _materialized(
            session, runner, matter, anchor,
            {"logical_title": "T", "status": "draft",
             "identity": "new_document", "confidence": 0.9},
        )
        _materialized(
            session, runner, matter, flip,
            {"logical_title": "T", "status": "draft", "identity": "new_version",
             "same_document_ref": anchor.id, "relative_order": "after",
             "confidence": 0.9},
        )
        assert _supersedes_pairs(session) != set()

        # the re-run decides it was a duplicate after all: the stale supersedes
        # between the merged pair collapses to a self-loop and must vanish
        _materialized(
            session, runner, matter, flip,
            {"logical_title": "T", "status": "draft", "identity": "duplicate",
             "duplicate_of": anchor.id, "confidence": 0.9},
        )
        session.commit()

        assert _supersedes_pairs(session) == set()
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
        assert linked_version(session, flip.id).id == linked_version(session, anchor.id).id


def test_revisions_digest_is_bounded_and_longest_first() -> None:
    from knowledge_index.pipeline.folder_context import revisions_digest

    assert revisions_digest(None, max_entries=10, max_chars=1500) is None
    assert revisions_digest([], max_entries=10, max_chars=1500) is None

    revisions = [
        {"kind": "ins", "text": "x" * 3, "author": "A", "date": "2025-01-01"},
        {"kind": "del", "text": "y" * 500, "author": "B", "date": "2025-01-02"},
        {"kind": "ins", "text": "z" * 50, "author": "C", "date": "2025-01-03"},
    ]
    digest = revisions_digest(revisions, max_entries=2, max_chars=4000)
    assert digest["count"] == 3
    assert len(digest["changes"]) == 2
    # longest first, and each entry's text capped at 200 chars
    assert digest["changes"][0]["author"] == "B"
    assert len(digest["changes"][0]["text"]) == 200
    assert digest["changes"][1]["author"] == "C"

    # the char budget stops sampling even when entries remain
    tight = revisions_digest(revisions, max_entries=10, max_chars=150)
    assert tight["count"] == 3
    assert len(tight["changes"]) == 1


def test_open_source_file_carries_tracked_changes_digest(session: Session) -> None:
    source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
    session.add(source)
    session.flush()
    content_hash = "e1" * 32
    session.add(Blob(content_hash=content_hash, size_bytes=1))
    session.flush()
    obj = SourceObject(
        source_id=source.id,
        external_id="M-1/markup.docx",
        path="M-1/markup.docx",
        name="markup.docx",
        content_hash=content_hash,
    )
    session.add(obj)
    session.add(
        Artifact(
            content_hash=content_hash,
            producer="test-converter",
            producer_version="1",
            kind="structured_json",
            payload={
                "text": "accepted view",
                "revisions": [
                    {"kind": "del", "text": "two (2) Business Days", "author": "O", "date": None}
                ],
            },
        )
    )
    session.flush()

    opened = open_source_file(session, source.id, "M-1/markup.docx")
    assert opened["tracked_changes"]["count"] == 1
    assert opened["tracked_changes"]["changes"][0]["text"] == "two (2) Business Days"
    # clean files carry no digest key at all
    clean = _converted_object(session, source, "M-1/clean.txt", "e2" * 32, "clean")
    assert "tracked_changes" not in open_source_file(session, source.id, clean.path)


def test_redline_by_sets_reverse_pointer_and_parks_when_unclassified(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        base = _assigned_object(session, source, matter, "M-1/base.docx", "f1" * 32, "base")
        markup = _assigned_object(
            session, source, matter, "M-1/markup.docx", "f2" * 32, "markup"
        )
        _materialized(
            session, runner, matter, markup,
            {"logical_title": "Agreement", "status": "draft",
             "identity": "new_document", "confidence": 0.9},
        )
        # the BASE's relate recognizes the neighbour as a markup of itself
        _materialized(
            session, runner, matter, base,
            {"logical_title": "Agreement", "status": "draft",
             "identity": "new_document", "redline_by": markup.id, "confidence": 0.9},
        )
        session.commit()
        assert linked_version(session, markup.id).redline_against == (
            linked_version(session, base.id).id
        )

        # and the parked variant: markup file exists but is not classified yet
        late_markup = _converted_object(
            session, source, "M-1/late-markup.docx", "f3" * 32, "late markup"
        )
        _materialized(
            session, runner, matter, base,
            {"logical_title": "Agreement", "status": "draft",
             "identity": "new_document", "redline_by": late_markup.id,
             "confidence": 0.9},
        )
        session.commit()
        intent = session.scalar(
            select(RelationIntent).where(RelationIntent.intent == "redline_by")
        )
        assert intent is not None and intent.status == "pending"

        session.add(
            MatterAssignment(
                source_object_id=late_markup.id,
                matter_id=matter.id,
                confidence=1,
                producer_version="test",
            )
        )
        session.flush()
        assert runner._apply_relation_intents(session, late_markup.id) == 1
        session.commit()
        assert linked_version(session, late_markup.id).redline_against == (
            linked_version(session, base.id).id
        )


def _email_artifact_object(
    session: Session,
    source: Source,
    path: str,
    content_hash: str,
    *,
    subject: str,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    sender: str = "a@x.example",
    to: str = "b@x.example",
    date: str | None = None,
) -> SourceObject:
    obj = SourceObject(
        source_id=source.id,
        external_id=path,
        path=path,
        name=path.rsplit("/", 1)[-1],
        content_hash=content_hash,
    )
    session.add(Blob(content_hash=content_hash, size_bytes=1))
    session.flush()
    session.add(obj)
    session.add(
        Artifact(
            content_hash=content_hash,
            producer="test-converter",
            producer_version="1",
            kind="structured_json",
            payload={
                "text": f"body of {path}",
                "metadata": {
                    "converter": "stdlib-email",
                    "subject": subject,
                    "from": sender,
                    "to": to,
                    "cc": None,
                    "date": date,
                    "message_id": message_id,
                    "in_reply_to": in_reply_to,
                    "references": references,
                },
            },
        )
    )
    session.flush()
    return obj


def test_normalize_subject_strips_reply_prefixes() -> None:
    from knowledge_index.pipeline.runner import _normalize_subject

    assert _normalize_subject("Re:  Deal   Alpha") == "Deal Alpha"
    assert _normalize_subject("AW: Re: Fwd: Deal Alpha") == "Deal Alpha"
    assert _normalize_subject("Rebate terms") == "Rebate terms"  # no false prefix
    assert _normalize_subject("") == ""


def test_message_id_linkage_joins_thread_regardless_of_arrival_order(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        original = _email_artifact_object(
            session, source, "M-1/original.eml", "a1" * 32,
            subject="Deal Alpha", message_id="<orig@x>", sender="ada@x.example",
            date="Mon, 10 Mar 2025 10:00:00 +0000",
        )
        reply = _email_artifact_object(
            session, source, "M-1/reply.eml", "a2" * 32,
            subject="Completely Different Subject Line",
            message_id="<reply@x>", in_reply_to="<orig@x>", sender="bo@y.example",
            date="Tue, 11 Mar 2025 09:00:00 +0000",
        )
        for obj in (original, reply):
            session.add(
                MatterAssignment(
                    source_object_id=obj.id, matter_id=matter.id,
                    confidence=1, producer_version="test",
                )
            )
        session.flush()

        # REPLY arrives first; its References carry the original's id
        for obj in (reply, original):
            _materialized(
                session, runner, matter, obj,
                {"logical_title": obj.name, "status": "final",
                 "identity": "new_document", "confidence": 0.9},
            )
        session.commit()

        threads = session.scalars(select(CommunicationThread)).all()
        assert len(threads) == 1
        thread = threads[0]
        assert set(thread.message_ids) == {"orig@x", "reply@x"}
        assert "ada@x.example" in thread.participants
        assert "bo@y.example" in thread.participants
        assert thread.time_range["from"].startswith("2025-03-10")
        assert thread.time_range["to"].startswith("2025-03-11")
        memberships = session.scalars(
            select(Relation).where(Relation.kind == "belongs_to_thread")
        ).all()
        assert len(memberships) == 2


def test_bridging_email_merges_thread_fragments(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        first = _email_artifact_object(
            session, source, "M-1/first.eml", "b1" * 32,
            subject="Opening", message_id="<a@x>",
        )
        last = _email_artifact_object(
            session, source, "M-1/last.eml", "b2" * 32,
            subject="Something else entirely", message_id="<c@x>",
        )
        bridge = _email_artifact_object(
            session, source, "M-1/bridge.eml", "b3" * 32,
            subject="Third subject", message_id="<b@x>",
            references="<a@x> <c@x>",
        )
        for obj in (first, last, bridge):
            session.add(
                MatterAssignment(
                    source_object_id=obj.id, matter_id=matter.id,
                    confidence=1, producer_version="test",
                )
            )
        session.flush()
        for obj in (first, last, bridge):  # bridge arrives after both fragments
            _materialized(
                session, runner, matter, obj,
                {"logical_title": obj.name, "status": "final",
                 "identity": "new_document", "confidence": 0.9},
            )
        session.commit()

        threads = session.scalars(select(CommunicationThread)).all()
        assert len(threads) == 1
        assert set(threads[0].message_ids) == {"a@x", "b@x", "c@x"}
        memberships = session.scalars(
            select(Relation).where(Relation.kind == "belongs_to_thread")
        ).all()
        assert len(memberships) == 3
        assert {m.to_id for m in memberships} == {threads[0].id}


def test_document_in_two_threads_unifies_them_without_headers(
    factory: sessionmaker[Session],
) -> None:
    """The live failure: two model calls invent different subjects for one
    conversation; no Message-IDs exist. Shared membership must merge the threads."""
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        cover = _email_artifact_object(
            session, source, "M-1/cover.eml", "c1" * 32,
            subject="Meridian Capital / Voss Bank — 2011 GMRA: Turn 2 Redline",
            sender="okonkwo@x.example",
        )
        reply = _email_artifact_object(
            session, source, "M-1/reply.eml", "c2" * 32,
            subject="Re: Meridian / Voss — Response to Mark-Ups",
            sender="fenn@y.example",
        )
        for obj in (cover, reply):
            session.add(
                MatterAssignment(
                    source_object_id=obj.id, matter_id=matter.id,
                    confidence=1, producer_version="test",
                )
            )
        session.flush()

        _materialized(
            session, runner, matter, cover,
            {"logical_title": "Cover", "status": "final",
             "identity": "new_document", "confidence": 0.9},
        )
        assert session.scalar(select(func.count()).select_from(CommunicationThread)) == 1

        # reply's model call names its own subject AND declares cover a member
        _materialized(
            session, runner, matter, reply,
            {"logical_title": "Reply", "status": "final",
             "identity": "new_document",
             "thread_subject": "whatever the model invented",
             "thread_member_refs": [cover.id],
             "confidence": 0.9},
        )
        session.commit()

        threads = session.scalars(select(CommunicationThread)).all()
        assert len(threads) == 1
        assert "okonkwo@x.example" in threads[0].participants
        assert "fenn@y.example" in threads[0].participants
        memberships = session.scalars(
            select(Relation).where(Relation.kind == "belongs_to_thread")
        ).all()
        assert {m.to_id for m in memberships} == {threads[0].id}
        assert len(memberships) == 2


def test_non_email_thread_subject_is_normalized_and_lone_email_gets_thread(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        lone = _email_artifact_object(
            session, source, "M-1/lone.eml", "d1" * 32,
            subject="Re: Status Update", sender="x@x.example",
            date="Wed, 12 Mar 2025 08:00:00 +0000",
        )
        session.add(
            MatterAssignment(
                source_object_id=lone.id, matter_id=matter.id,
                confidence=1, producer_version="test",
            )
        )
        letter = _assigned_object(
            session, source, matter, "M-1/letter.pdf", "d2" * 32, "letter body"
        )
        session.flush()

        # a lone email threads itself even when the model returns no thread fields
        _materialized(
            session, runner, matter, lone,
            {"logical_title": "Lone", "status": "final",
             "identity": "new_document", "confidence": 0.9},
        )
        thread = session.scalar(select(CommunicationThread))
        assert thread is not None
        assert thread.subject_norm == "Status Update"
        assert thread.participants and thread.time_range is not None

        # a non-email with a model subject joins after code-side normalization
        _materialized(
            session, runner, matter, letter,
            {"logical_title": "Letter", "status": "final",
             "identity": "new_document",
             "thread_subject": "AW: Re: Status   Update", "confidence": 0.9},
        )
        session.commit()
        assert session.scalar(select(func.count()).select_from(CommunicationThread)) == 1


def test_list_one_folder_opens_siblings_and_reports_unknowns(session: Session) -> None:
    from knowledge_index.pipeline.folder_context import list_one_folder

    source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
    session.add(source)
    session.flush()
    _converted_object(
        session, source, "Mandate/M-1/Correspondence/cover.eml", "01" * 32, "x"
    )
    _converted_object(session, source, "Mandate/M-1/Drafts/draft-1.docx", "02" * 32, "x")
    _converted_object(
        session, source, "Mandate/M-1/Drafts/redline.docx", "03" * 32, "x"
    )
    _converted_object(
        session, source, "Mandate/M-1/Drafts/Annexes/annex-a.docx", "04" * 32, "x"
    )

    listing = list_one_folder(session, source.id, "Mandate/M-1/Drafts")
    assert "draft-1.docx" in listing and "redline.docx" in listing
    assert "Annexes/" in listing  # subfolder names revealed -> transitive reach
    assert "annex-a.docx" not in listing  # direct contents only

    root = list_one_folder(session, source.id, "/")
    assert "Mandate/" in root

    assert "no such folder" in list_one_folder(session, source.id, "Mandate/M-9")

    capped = list_one_folder(session, source.id, "Mandate/M-1/Drafts", per_folder_limit=1)
    assert "(+1 more files)" in capped


def test_title_belongs_to_the_newest_version_not_the_last_arrival(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        matter = Matter(title="M-1", reference_numbers=["M-1"], imported=False)
        session.add_all([source, matter])
        session.flush()
        runner = PipelineRunner(factory, AppConfig())
        newest = _assigned_object(session, source, matter, "M-1/v2.txt", "aa" * 32, "v2")
        older = _assigned_object(session, source, matter, "M-1/v1.txt", "bb" * 32, "v1")

        _materialized(
            session, runner, matter, newest,
            {"logical_title": "Agreement (final naming)", "status": "final",
             "identity": "new_document", "confidence": 0.9},
        )
        # the OLDER draft arrives later with its own idea of the title
        _materialized(
            session, runner, matter, older,
            {"logical_title": "Agreement (working title)", "status": "draft",
             "identity": "new_version", "same_document_ref": newest.id,
             "relative_order": "before", "confidence": 0.9},
        )
        session.commit()
        document = linked_document(session, newest.id)
        assert document.title == "Agreement (final naming)"  # no last-arrival churn

        # but a genuinely NEWER version may rename the chain
        newer_still = _assigned_object(
            session, source, matter, "M-1/v3.txt", "cc" * 32, "v3"
        )
        _materialized(
            session, runner, matter, newer_still,
            {"logical_title": "Agreement (renamed in v3)", "status": "final",
             "identity": "new_version", "same_document_ref": newest.id,
             "relative_order": "after", "confidence": 0.9},
        )
        session.commit()
        document = linked_document(session, newest.id)
        assert document.title == "Agreement (renamed in v3)"
