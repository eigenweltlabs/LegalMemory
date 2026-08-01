from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

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
    SourceGroupMember,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.sync import LocalFilesystemSource, SyncEngine
from knowledge_index.sync.base import (
    SourceCapabilities,
    SourceObjectObservation,
    UnsupportedOperation,
)
from knowledge_index.sync import deletions
from knowledge_index.pipeline import PipelineRunner
from knowledge_index.taxonomies import ACCESS_ONLY_REINDEX


def add_source(session: Session, root: Path) -> Source:
    source = Source(kind="local_fs", display_name="fixture", config={"root": str(root)})
    session.add(source)
    session.flush()
    return source


def test_full_sync_is_idempotent_and_tombstones_only_after_complete_scan(
    session: Session, tmp_path: Path
) -> None:
    (tmp_path / "Mandate").mkdir()
    first = tmp_path / "Mandate" / "Vertrag_final.txt"
    first.write_text("Haftung ist begrenzt.", encoding="utf-8")
    source = add_source(session, tmp_path)

    result = SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    assert (result.created, result.tombstoned) == (1, 0)

    again = SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    assert (again.unchanged, again.created) == (1, 0)

    first.unlink()
    deleted = SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    assert deleted.tombstoned == 1
    row = session.scalar(select(SourceObject))
    assert row is not None and row.deleted_at is not None


def test_change_resets_pipeline_and_restore_reuses_source_object(
    session: Session, tmp_path: Path
) -> None:
    document = tmp_path / "Entwurf.txt"
    document.write_text("Version eins", encoding="utf-8")
    source = add_source(session, tmp_path)
    SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()

    source_object = session.scalar(select(SourceObject))
    assert source_object is not None
    source_object.content_hash = "a" * 64
    states = session.scalars(select(ProcessingState)).all()
    for state in states:
        state.status = "done"

    document.write_text("Version zwei mit mehr Inhalt", encoding="utf-8")
    result = SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    assert result.changed == 1
    assert source_object.content_hash is None
    state_by_stage = {state.stage: state.status for state in states}
    assert state_by_stage["fetch"] == "pending"
    assert state_by_stage["convert"] == "skipped"

    document.unlink()
    SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    document.write_text("Wiederhergestellt", encoding="utf-8")
    restored = SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    assert restored.restored == 1
    assert session.scalars(select(SourceObject)).all() == [source_object]


def test_local_connector_never_follows_or_fetches_escape_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    connector = LocalFilesystemSource(tmp_path)

    assert list(connector.full_scan()) == []
    with pytest.raises(ValueError):
        connector.fetch("../secret.txt")
    with pytest.raises(FileNotFoundError):
        connector.fetch("link.txt")


def test_acl_resolver_is_snapshotted(session: Session, tmp_path: Path) -> None:
    (tmp_path / "ethical-wall.txt").write_text("restricted", encoding="utf-8")
    connector = LocalFilesystemSource(
        tmp_path,
        acl_resolver=lambda _: [
            {"principal": "group:ma-team", "principal_kind": "group", "access": "allow"}
        ],
    )
    source = add_source(session, tmp_path)
    SyncEngine(session, source, connector).sync()

    row = session.scalar(select(SourceObject))
    assert row is not None
    assert row.acl == [{"principal": "group:ma-team", "principal_kind": "group", "access": "allow"}]


def test_repeat_sync_with_identical_acls_is_idempotent(session: Session, tmp_path: Path) -> None:
    (tmp_path / "akte.txt").write_text("Inhalt", encoding="utf-8")
    acl = [{"principal": "group:litigation", "principal_kind": "group", "access": "allow"}]
    connector = LocalFilesystemSource(tmp_path, acl_resolver=lambda _: list(acl))
    source = add_source(session, tmp_path)

    for _ in range(3):
        SyncEngine(session, source, connector).sync()
        session.commit()

    grants = session.scalars(select(SourceObjectGrant)).all()
    assert [(g.principal, g.effect) for g in grants] == [("group:litigation", "allow")]


def test_full_sync_persists_the_connector_checkpoint(session: Session, tmp_path: Path) -> None:
    source = add_source(session, tmp_path)

    class CursorSeedingConnector(LocalFilesystemSource):
        def cursor_state(self) -> str:
            return '{"delta_token": "next"}'

    SyncEngine(
        session,
        source,
        CursorSeedingConnector(tmp_path),
    ).sync()

    assert source.cursor == '{"delta_token": "next"}'


class _StagedConnector:
    """API connector whose observation already points at the downloaded bytes."""

    def __init__(
        self,
        kind: str,
        staged_path: Path,
        *,
        path: str,
        mtime: datetime,
        acl: list[dict],
    ) -> None:
        self.kind = kind
        self.staged_path = staged_path
        self.path = path
        self.mtime = mtime
        self.acl = acl
        self.capabilities = SourceCapabilities(
            delta=False,
            webhooks=False,
            acl=True,
            versions=False,
            stable_ids=True,
            verifiable_emptiness=True,
        )

    def full_scan(self):
        yield SourceObjectObservation(
            external_id="remote-file-1",
            path=self.path,
            name=self.staged_path.name,
            size_bytes=self.staged_path.stat().st_size,
            mtime=self.mtime,
            source_version_label=self.mtime.isoformat(),
            acl=self.acl,
            staged_path=str(self.staged_path),
        )

    def changes(self, cursor):
        raise UnsupportedOperation

    def fetch(self, external_id):
        return self.staged_path.open("rb")


@pytest.mark.parametrize("kind", ["google_drive", "sharepoint_online"])
def test_staged_acl_and_metadata_change_requeues_only_access_index(
    session: Session, tmp_path: Path, kind: str
) -> None:
    staged = tmp_path / "contract.docx"
    staged.write_bytes(b"identical provider bytes")
    first_mtime = datetime(2026, 7, 1, tzinfo=UTC)
    old_acl = [
        {
            "principal": "user:owner@example.com",
            "principal_kind": "user",
            "access": "allow",
            "origin": "connector",
        }
    ]
    new_acl = [
        *old_acl,
        {
            "principal": "user:reader@example.com",
            "principal_kind": "user",
            "access": "allow",
            "origin": "connector",
        },
    ]
    source = Source(kind=kind, display_name=kind, config={})
    session.add(source)
    session.flush()
    SyncEngine(
        session,
        source,
        _StagedConnector(
            kind,
            staged,
            path="Shared drive/old/contract.docx",
            mtime=first_mtime,
            acl=old_acl,
        ),
    ).sync()
    source_object = session.scalar(select(SourceObject))
    assert source_object is not None
    expected_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
    source_object.content_hash = expected_hash
    states = session.scalars(select(ProcessingState)).all()
    for state in states:
        state.status = "done"
        state.last_error = None

    result = SyncEngine(
        session,
        source,
        _StagedConnector(
            kind,
            staged,
            path="Shared drive/new/contract.docx",
            mtime=first_mtime + timedelta(minutes=1),
            acl=new_acl,
        ),
    ).sync()

    assert result.changed == 0
    assert result.metadata_changed == 1
    assert result.access_changed == 1
    assert result.unchanged == 1
    assert source_object.content_hash == expected_hash
    assert source_object.path == "Shared drive/new/contract.docx"
    states_by_stage = {state.stage: state for state in states}
    assert states_by_stage["index"].status == "pending"
    assert states_by_stage["index"].last_error == {"reason": ACCESS_ONLY_REINDEX}
    assert {
        state.status for stage, state in states_by_stage.items() if stage != "index"
    } == {"done"}
    assert {
        row.principal for row in session.scalars(select(SourceObjectGrant)).all()
    } == {"user:owner@example.com", "user:reader@example.com"}


def test_staged_byte_change_resets_full_pipeline_even_without_metadata_change(
    session: Session, tmp_path: Path
) -> None:
    staged = tmp_path / "contract.docx"
    staged.write_bytes(b"version one")
    stamp = datetime(2026, 7, 1, tzinfo=UTC)
    source = Source(kind="sharepoint_online", display_name="SharePoint", config={})
    session.add(source)
    session.flush()
    connector = _StagedConnector(
        "sharepoint_online",
        staged,
        path="Site/contract.docx",
        mtime=stamp,
        acl=[],
    )
    SyncEngine(session, source, connector).sync()
    source_object = session.scalar(select(SourceObject))
    assert source_object is not None
    source_object.content_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
    states = session.scalars(select(ProcessingState)).all()
    for state in states:
        state.status = "done"

    staged.write_bytes(b"version two")
    result = SyncEngine(session, source, connector).sync()

    assert result.changed == 1
    assert source_object.content_hash is None
    states_by_stage = {state.stage: state.status for state in states}
    assert states_by_stage["fetch"] == "pending"
    assert states_by_stage["convert"] == "skipped"


def test_access_only_index_refresh_preserves_embeddings(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch
) -> None:
    content_hash = hashlib.sha256(b"indexed text").hexdigest()
    with factory() as session:
        source = Source(kind="sharepoint_online", display_name="SharePoint", config={})
        session.add(source)
        session.flush()
        source_object = SourceObject(
            source_id=source.id,
            external_id="remote-file-1",
            path="Site/contract.docx",
            name="contract.docx",
            content_hash=content_hash,
            acl=[],
        )
        session.add_all(
            [source_object, Blob(content_hash=content_hash, size_bytes=len(b"indexed text"))]
        )
        session.flush()
        session.add(
            Artifact(
                content_hash=content_hash,
                producer="test",
                producer_version="1",
                kind="structured_json",
                payload={"text": "indexed text"},
            )
        )
        session.flush()
        document = Document(title="Contract", language="en")
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            content_hash=content_hash,
            status="final",
        )
        session.add(version)
        session.flush()
        document.latest_final_version_id = version.id
        session.add_all(
            [
                DocumentVersionSource(
                    version_id=version.id,
                    source_object_id=source_object.id,
                ),
                SourceObjectGrant(
                    source_object_id=source_object.id,
                    principal="user:reader@example.com",
                    principal_kind="user",
                    effect="allow",
                    origin="connector",
                ),
                Chunk(
                    document_version_id=version.id,
                    ordinal=0,
                    text="indexed text",
                    meta={"source_object_id": source_object.id, "kind": "chunk"},
                    document_id=document.id,
                    version_status="final",
                    language="en",
                    allowed_principals=["user:owner@example.com"],
                    denied_principals=[],
                    access_version=1,
                    embedding=[0.25, 0.5],
                    embedding_model="existing-model",
                ),
                ProcessingState(
                    source_object_id=source_object.id,
                    stage="index",
                    status="pending",
                    last_error={"reason": ACCESS_ONLY_REINDEX},
                ),
            ]
        )
        session.commit()
        source_object_id = source_object.id

    def unexpected_embedding(*_args, **_kwargs):
        raise AssertionError("an ACL-only refresh must not call the embedding model")

    indexed: list[Chunk] = []

    def capture_bulk(_index, *, deletes, upserts):
        assert deletes == []
        indexed.extend(upserts)

    monkeypatch.setattr("knowledge_index.pipeline.runner.embed_text", unexpected_embedding)
    monkeypatch.setattr(
        "knowledge_index.search_backend.OpenSearchIndex.bulk_sync",
        capture_bulk,
    )
    result = PipelineRunner(
        factory,
        AppConfig(artifact_dir=tmp_path / "artifacts"),
    ).run_stage_for_object("index", source_object_id)

    assert result.done == 1
    assert len(indexed) == 1
    with factory() as session:
        chunk = session.scalar(select(Chunk))
        state = session.scalar(
            select(ProcessingState).where(ProcessingState.stage == "index")
        )
        assert chunk is not None
        assert chunk.embedding == [0.25, 0.5]
        assert chunk.embedding_model == "existing-model"
        assert chunk.allowed_principals == ["user:reader@example.com"]
        assert chunk.access_version == 2
        assert state is not None
        assert state.status == "done"
        assert state.last_error is None


def test_document_stage_runner_resumes_one_object_without_touching_others(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second", encoding="utf-8")
    with factory() as session:
        source = add_source(session, tmp_path)
        SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
        session.commit()
        object_ids = session.scalars(
            select(SourceObject.id).order_by(SourceObject.path)
        ).all()

    runner = PipelineRunner(factory, AppConfig(artifact_dir=tmp_path / "artifacts"))
    first = runner.run_stage_for_object("fetch", object_ids[0])
    replay = runner.run_stage_for_object("fetch", object_ids[0])

    assert first.done == 1
    assert replay.processed == 0  # durable done state: no refetch on workflow replay
    with factory() as session:
        states = {
            (row.source_object_id, row.stage): row.status
            for row in session.scalars(select(ProcessingState)).all()
        }
    assert states[(object_ids[0], "fetch")] == "done"
    assert states[(object_ids[0], "convert")] == "pending"
    assert states[(object_ids[1], "fetch")] == "pending"


# --------------------------------------------------------------- confirmed deletions
#
# An API-backed connector cannot tell "the source is empty" from "the source refused
# me". Tombstoning on that signal deletes a firm's whole index from a sync that reported
# success — but refusing the scan outright left a real bulk deletion applyable only from
# psql. So a deletion that large is held and applied once consecutive scans agree on the
# identical set of missing objects.


class _ApiConnector:
    """Stands in for an API-backed source: emptiness is never provable."""

    kind = "sharepoint_online"

    def __init__(self, external_ids: list[str]) -> None:
        self._external_ids = external_ids
        self.capabilities = SourceCapabilities(
            delta=False, webhooks=False, acl=True, versions=False, stable_ids=True
        )

    def full_scan(self):
        for external_id in self._external_ids:
            yield SourceObjectObservation(
                external_id=external_id,
                path=f"Mandate/{external_id}.docx",
                name=f"{external_id}.docx",
                acl=[{"principal": "group:entra:abc", "principal_kind": "group", "effect": "allow"}],
                staged_path=f"/staged/{external_id}",
            )

    def changes(self, cursor):
        raise UnsupportedOperation("no delta")

    def fetch(self, external_id):
        raise UnsupportedOperation("staged")


class _MembershipConnector(_ApiConnector):
    def __init__(self, memberships: list[dict]) -> None:
        super().__init__(["a"])
        self._memberships = memberships

    def memberships(self) -> list[dict]:
        return list(self._memberships)


def _api_source(session: Session) -> Source:
    source = Source(kind="sharepoint_online", display_name="SharePoint", config={})
    session.add(source)
    session.flush()
    return source


def test_an_empty_membership_snapshot_revokes_the_previous_snapshot(session: Session) -> None:
    source = _api_source(session)
    SyncEngine(
        session,
        source,
        _MembershipConnector(
            [
                {
                    "group_id": "entra:team-1",
                    "member_id": "former.member@example.com",
                    "member_type": "user",
                }
            ]
        ),
    ).sync()
    assert session.scalar(select(func.count()).select_from(SourceGroupMember)) == 1

    # Empty means the source now proves no membership. Retaining the old edge would let a
    # removed member keep retrieving group-granted documents indefinitely.
    SyncEngine(session, source, _MembershipConnector([])).sync()
    assert session.scalar(select(func.count()).select_from(SourceGroupMember)) == 0


class _CheapMembershipDeltaConnector(_MembershipConnector):
    """A delta-capable source whose group expansion is cheap (e.g. Clio)."""

    cheap_memberships = True

    def __init__(self, memberships: list[dict]) -> None:
        super().__init__(memberships)
        self.capabilities = SourceCapabilities(
            delta=True, webhooks=False, acl=True, versions=False, stable_ids=True
        )

    def changes(self, cursor):
        from knowledge_index.sync.base import ChangeBatch

        return ChangeBatch(
            observations=[], deleted_external_ids=[], next_cursor="{}", has_more=False
        )

    def cursor_state(self):
        return "{}"


def test_cheap_membership_connectors_refresh_memberships_on_incremental_syncs(
    session: Session,
) -> None:
    """A wall change must land at the policy interval, not the daily full refresh.

    Group expansion normally runs only after full scans because a Graph tenant
    expansion is expensive. A connector that declares its expansion cheap gets the
    refresh on every sync, so adding someone to (or removing them from) a source
    group takes effect on the next scheduled run.
    """
    source = _api_source(session)
    # A recent full scan, so the engine genuinely takes the incremental path.
    source.last_full_sync_at = datetime.now(UTC)
    member = {"group_id": "clio:5", "member_id": "anwalt@kanzlei.de", "member_type": "user"}
    result = SyncEngine(session, source, _CheapMembershipDeltaConnector([member])).sync()
    assert result.mode == "incremental"
    assert session.scalar(select(func.count()).select_from(SourceGroupMember)) == 1

    # The person left the group; the next incremental run must revoke the edge.
    result = SyncEngine(session, source, _CheapMembershipDeltaConnector([])).sync()
    assert result.mode == "incremental"
    assert session.scalar(select(func.count()).select_from(SourceGroupMember)) == 0


def test_empty_api_scan_does_not_tombstone_the_whole_source(session: Session) -> None:
    source = _api_source(session)
    SyncEngine(session, source, _ApiConnector(["a", "b", "c"])).sync()
    assert session.scalar(select(func.count()).select_from(SourceObject)) == 3

    # A revoked scope looks exactly like this. The previous index must survive the scan
    # that reports it, and the sync itself is a success, not an error.
    result = SyncEngine(session, source, _ApiConnector([])).sync()
    assert (result.tombstoned, result.pending_deletions) == (0, 3)
    assert (result.deletion_confirmations, result.deletion_confirmations_required) == (1, 3)

    live = session.scalars(select(SourceObject)).all()
    assert [row.deleted_at for row in live] == [None, None, None]
    assert session.get(Source, source.id).status == "active"


def test_partial_api_scan_does_not_tombstone_the_majority(session: Session) -> None:
    source = _api_source(session)
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything)).sync()

    # One site of many enumerating is not evidence the rest were deleted.
    result = SyncEngine(session, source, _ApiConnector(everything[:5])).sync()
    assert (result.tombstoned, result.pending_deletions) == (0, 25)
    assert not any(row.deleted_at for row in session.scalars(select(SourceObject)).all())


def test_a_small_deletion_still_tombstones_on_the_very_next_scan(session: Session) -> None:
    """The common case must not get slower because the rare one got safer."""
    source = _api_source(session)
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything)).sync()

    result = SyncEngine(session, source, _ApiConnector(everything[:29])).sync()
    assert (result.tombstoned, result.pending_deletions) == (1, 0)
    assert deletions.pending(session, source.id) is None


def test_a_large_deletion_applies_once_three_scans_agree(session: Session) -> None:
    source = _api_source(session)
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything)).sync()
    survivors = everything[:5]

    first = SyncEngine(session, source, _ApiConnector(survivors)).sync()
    assert (first.tombstoned, first.deletion_confirmations) == (0, 1)
    second = SyncEngine(session, source, _ApiConnector(survivors)).sync()
    assert (second.tombstoned, second.deletion_confirmations) == (0, 2)
    # The documents answer searches throughout: nothing is removed on evidence this thin.
    assert not any(row.deleted_at for row in session.scalars(select(SourceObject)).all())

    third = SyncEngine(session, source, _ApiConnector(survivors)).sync()
    assert (third.tombstoned, third.pending_deletions) == (25, 0)
    assert deletions.pending(session, source.id) is None
    live = session.scalars(select(SourceObject).where(SourceObject.deleted_at.is_(None))).all()
    assert sorted(row.external_id for row in live) == sorted(survivors)


def test_objects_that_come_back_discard_the_confirmation(session: Session) -> None:
    """A connector that loses an estate once and returns it must never delete anything."""
    source = _api_source(session)
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything)).sync()

    SyncEngine(session, source, _ApiConnector(everything[:5])).sync()
    assert deletions.pending(session, source.id).confirmations == 1

    SyncEngine(session, source, _ApiConnector(everything)).sync()
    assert deletions.pending(session, source.id) is None

    # The next bad scan starts from one again, so three consecutive scans are still
    # required — a flapping connector can never accumulate its way to a deletion.
    again = SyncEngine(session, source, _ApiConnector(everything[:5])).sync()
    assert (again.tombstoned, again.deletion_confirmations) == (0, 1)


def test_a_different_missing_set_restarts_the_count(session: Session) -> None:
    """340 missing today and a different 340 tomorrow is garbage, not a deletion.

    Counting how many objects were missing would average the two into a confirmed
    deletion of documents that still exist, so the *set* is what has to match.
    """
    source = _api_source(session)
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything)).sync()

    SyncEngine(session, source, _ApiConnector(everything[:5])).sync()
    # Same number missing, different objects: not the same claim.
    second = SyncEngine(session, source, _ApiConnector(everything[25:])).sync()
    assert (second.tombstoned, second.deletion_confirmations) == (0, 1)
    third = SyncEngine(session, source, _ApiConnector(everything[:5])).sync()
    assert (third.tombstoned, third.deletion_confirmations) == (0, 1)
    assert not any(row.deleted_at for row in session.scalars(select(SourceObject)).all())


def test_the_number_of_confirmations_is_configurable(session: Session) -> None:
    source = _api_source(session)
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything)).sync()

    engine = SyncEngine(
        session, source, _ApiConnector(everything[:5]), deletion_confirmations=2
    )
    assert engine.sync().tombstoned == 0
    result = SyncEngine(
        session, source, _ApiConnector(everything[:5]), deletion_confirmations=2
    ).sync()
    assert result.tombstoned == 25


def test_raising_the_tombstone_fraction_skips_confirmation(session: Session) -> None:
    """The existing per-source override still decides immediately, not after N syncs."""
    source = _api_source(session)
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything)).sync()

    source.sync_policy = {**(source.sync_policy or {}), "max_tombstone_fraction": 0.95}
    session.flush()
    assert SyncEngine(session, source, _ApiConnector(everything[:5])).sync().tombstoned == 25


def test_a_real_bulk_deletion_can_be_confirmed_per_source(session: Session) -> None:
    source = _api_source(session)
    SyncEngine(session, source, _ApiConnector(["a", "b", "c"])).sync()

    source.sync_policy = {**(source.sync_policy or {}), "allow_empty_scan": True}
    session.flush()
    result = SyncEngine(session, source, _ApiConnector([])).sync()
    assert result.tombstoned == 3


def test_local_source_may_still_empty_itself(session: Session, tmp_path: Path) -> None:
    # A directory listing proves the files are gone, so the guard must not fire here.
    target = tmp_path / "Vertrag.txt"
    target.write_text("x", encoding="utf-8")
    source = add_source(session, tmp_path)
    SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    target.unlink()
    assert SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync().tombstoned == 1


def test_full_sync_records_when_acls_were_last_reread(session: Session, tmp_path: Path) -> None:
    source = add_source(session, tmp_path)
    SyncEngine(session, source, LocalFilesystemSource(tmp_path)).sync()
    assert session.get(Source, source.id).last_full_sync_at is not None


# ------------------------------------------------------------------------- re-scoping
#
# Narrowing which folders a source syncs looks exactly like a connector failing to
# enumerate: far fewer objects than are indexed. The difference is that a re-scope is an
# instruction, so it must be allowed to remove what fell outside — and only then.


def _scoped_config(*root_ids: str) -> dict:
    return {"connector": {"roots": [{"id": root_id} for root_id in root_ids]}}


def test_narrowing_the_scope_is_allowed_to_remove_what_fell_outside(session: Session) -> None:
    from knowledge_index.connectors import scoping

    source = _api_source(session)
    source.config = _scoped_config("folder-a", "folder-b")
    session.flush()
    wide = scoping.fingerprint(source.config["connector"])

    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything), selection_fingerprint=wide).sync()
    assert session.get(Source, source.id).selection_fingerprint == wide

    # The operator drops one of the two roots. Without the re-scope signal the tombstone
    # guard would refuse this, because it is indistinguishable from a broken scan.
    source.config = _scoped_config("folder-a")
    session.flush()
    narrowed = scoping.fingerprint(source.config["connector"])
    result = SyncEngine(
        session, source, _ApiConnector(everything[:5]), selection_fingerprint=narrowed
    ).sync()

    assert result.tombstoned == 25
    assert session.get(Source, source.id).selection_fingerprint == narrowed


def test_an_unchanged_scope_still_confirms_a_suspicious_drop(session: Session) -> None:
    from knowledge_index.connectors import scoping

    source = _api_source(session)
    source.config = _scoped_config("folder-a")
    session.flush()
    fingerprint = scoping.fingerprint(source.config["connector"])

    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything), selection_fingerprint=fingerprint).sync()

    # Same scope, far fewer objects: that is a broken scan until another scan says
    # otherwise, so nothing is removed on the strength of this one.
    result = SyncEngine(
        session, source, _ApiConnector(everything[:5]), selection_fingerprint=fingerprint
    ).sync()
    assert (result.tombstoned, result.pending_deletions) == (0, 25)
    assert not any(row.deleted_at for row in session.scalars(select(SourceObject)).all())


def test_reordering_roots_is_not_a_rescope(session: Session) -> None:
    # A fingerprint that moved when the UI merely reordered the list would force a full
    # rebuild of the firm's index for nothing.
    from knowledge_index.connectors import scoping

    assert scoping.fingerprint({"roots": [{"id": "a"}, {"id": "b"}]}) == scoping.fingerprint(
        {"roots": [{"id": "b"}, {"id": "a"}]}
    )


def test_widening_the_scope_forces_a_full_scan_not_a_delta(session: Session) -> None:
    """A delta token only describes changes inside the old scope.

    Resuming from it after adding a folder would never enumerate the newly included
    documents, so the source would look synced while silently missing them.
    """
    from knowledge_index.connectors import scoping

    source = _api_source(session)
    source.config = _scoped_config("folder-a")
    session.flush()
    source.cursor = '{"token": "old"}'
    source.selection_fingerprint = scoping.fingerprint(source.config["connector"])
    session.flush()

    source.config = _scoped_config("folder-a", "folder-b")
    session.flush()
    widened = scoping.fingerprint(source.config["connector"])

    engine = SyncEngine(
        session, source, _ApiConnector(["a", "b"]), selection_fingerprint=widened
    )
    assert engine._rescoped() is True
    assert engine.sync().mode == "full"


def test_an_unrecorded_fingerprint_is_not_treated_as_a_rescope(
    session: Session,
) -> None:
    """A source with no fingerprint recorded still has to confirm a large deletion.

    Nobody has changed the selection, so there is no instruction to remove anything.
    Reading a missing value as a change would apply the removal immediately on the run
    where an empty result is most likely to mean a credential that never worked.
    """
    from knowledge_index.connectors import scoping

    source = _api_source(session)
    source.config = _scoped_config("folder-a")
    session.flush()
    fingerprint = scoping.fingerprint(source.config["connector"])

    # Populate an index, then clear the fingerprint: rows exist, but the source has no
    # recorded selection to compare against.
    everything = [f"doc-{index}" for index in range(30)]
    SyncEngine(session, source, _ApiConnector(everything), selection_fingerprint=fingerprint).sync()
    source.selection_fingerprint = None
    session.flush()

    engine = SyncEngine(
        session, source, _ApiConnector([]), selection_fingerprint=fingerprint
    )
    assert engine._rescoped() is False
    result = engine.sync()
    assert (result.tombstoned, result.pending_deletions) == (0, 30)


def test_the_fingerprint_is_recorded_even_when_the_sync_stays_incremental(
    session: Session,
) -> None:
    """A source that never runs a full scan still needs a fingerprint to compare against."""
    from knowledge_index.connectors import scoping

    source = _api_source(session)
    source.config = _scoped_config("folder-a")
    session.flush()
    fingerprint = scoping.fingerprint(source.config["connector"])

    SyncEngine(session, source, _ApiConnector(["a"]), selection_fingerprint=fingerprint).sync()
    source.selection_fingerprint = None
    session.flush()

    SyncEngine(session, source, _ApiConnector(["a"]), selection_fingerprint=fingerprint).sync()
    assert session.get(Source, source.id).selection_fingerprint == fingerprint
