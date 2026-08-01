"""Deleting a connection has to take the firm's bytes with it — and only those.

The failure this guards against is not disk usage. An administrator disconnects a
client's SharePoint site, tells the partner the documents are gone, and the staged
originals stay readable on the volume for as long as the appliance lives. Every test
here is written from that sentence: what was reclaimed, what survived because something
else still needs it, and what the response says when a file refuses to go.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.artifacts import LocalArtifactStore
from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import (
    Artifact,
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Source,
    SourceObject,
)
from knowledge_index.web.app import create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}


@pytest.fixture
def appliance(factory: sessionmaker[Session], tmp_path: Path, monkeypatch):
    """A client, its config, and the artifact volume the connectors stage onto."""
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setenv("KI_ARTIFACT_DIR", str(artifact_dir))
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(artifact_dir=artifact_dir)
    config.components.orchestrator_provider = "local"
    store.save(config)
    return TestClient(create_app(factory, store)), config


def _stage(artifact_dir: Path, source_id: str, name: str, body: bytes) -> Path:
    """Write one staged copy exactly where the connector runtime would put it."""
    from knowledge_index.connectors.registry import staging_root_for_source

    target = staging_root_for_source(source_id) / "ab" / "abcd" / "v1" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    assert target.is_relative_to(artifact_dir)
    return target


def _blob(config: AppConfig, body: bytes) -> tuple[str, Path]:
    import hashlib
    import io

    stored = LocalArtifactStore(config.artifact_dir).put_blob(
        io.BytesIO(body), max_bytes=1_000_000
    )
    assert stored.content_hash == hashlib.sha256(body).hexdigest()
    return stored.content_hash, stored.path


def _source(session: Session, name: str, **config) -> Source:
    source = Source(kind="local_fs", display_name=name, config=config)
    session.add(source)
    session.flush()
    return source


def _object(session: Session, source: Source, path: str, content_hash: str | None) -> SourceObject:
    row = SourceObject(
        source_id=source.id,
        external_id=f"{source.id}:{path}",
        path=path,
        name=Path(path).name,
        content_hash=content_hash,
    )
    session.add(row)
    session.flush()
    return row


def test_delete_reclaims_staged_copies_and_unshared_blobs(appliance, factory, tmp_path):
    client, config = appliance
    shared_bytes = b"one contract, filed in two matters"
    private_bytes = b"only this client's engagement letter"
    shared_hash, shared_path = _blob(config, shared_bytes)
    private_hash, private_path = _blob(config, private_bytes)

    with factory() as session:
        doomed = _source(session, "Client A site")
        keeper = _source(session, "Client B site")
        _object(session, doomed, "/a/shared.pdf", shared_hash)
        _object(session, doomed, "/a/private.pdf", private_hash)
        _object(session, keeper, "/b/shared.pdf", shared_hash)
        for content_hash, body in ((shared_hash, shared_bytes), (private_hash, private_bytes)):
            session.add(Blob(content_hash=content_hash, size_bytes=len(body)))
            session.flush()
            session.add(
                Artifact(
                    content_hash=content_hash,
                    producer="docling-serve",
                    producer_version="1",
                    kind="structured_json",
                    payload={"text": "the document's full text"},
                )
            )
        session.commit()
        doomed_id, keeper_id = doomed.id, keeper.id

    staged = _stage(config.artifact_dir, doomed_id, "private.pdf", private_bytes)
    survivor = _stage(config.artifact_dir, keeper_id, "shared.pdf", shared_bytes)

    response = client.delete(f"/api/sources/{doomed_id}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    storage = response.json()["storage"]

    assert storage["complete"] is True
    assert storage["failures"] == []
    # The shared contract is one of the two hashes this source held, and it stayed.
    assert storage["blobs_retained_shared"] == 1
    assert storage["blobs_removed"] == 1
    assert storage["bytes_reclaimed"] >= len(private_bytes) * 2

    assert not staged.exists()
    assert not staged.parent.parent.parent.exists()  # the whole per-source tree went
    assert not private_path.exists()
    assert shared_path.exists(), "a blob another source still references must survive"
    assert survivor.exists(), "another source's staged content is not this delete's business"

    with factory() as session:
        assert session.get(Blob, private_hash) is None
        assert session.get(Blob, shared_hash) is not None
        remaining = {row.content_hash for row in session.query(Artifact).all()}
        assert remaining == {shared_hash}


def test_blob_held_by_an_orphan_version_keeps_its_row_but_loses_its_bytes(appliance, factory):
    """A version left behind by design must not be left pointing at a live file."""
    client, config = appliance
    body = b"a draft nobody else has"
    content_hash, blob_path = _blob(config, body)

    with factory() as session:
        source = _source(session, "Client C site")
        obj = _object(session, source, "/c/draft.docx", content_hash)
        session.add(Blob(content_hash=content_hash, size_bytes=len(body), cached_path=str(blob_path)))
        document = Document(title="Draft")
        session.add(document)
        session.flush()
        version = DocumentVersion(document_id=document.id, content_hash=content_hash)
        session.add(version)
        session.flush()
        session.add(DocumentVersionSource(version_id=version.id, source_object_id=obj.id))
        session.commit()
        source_id = source.id

    response = client.delete(f"/api/sources/{source_id}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["storage"]["complete"] is True

    assert not blob_path.exists()
    with factory() as session:
        blob = session.get(Blob, content_hash)
        assert blob is not None, "the orphaned version still points at this row"
        assert blob.cached_path is None, "nothing may read a path that no longer exists"


def test_a_version_another_source_still_vouches_for_keeps_its_blob(appliance, factory):
    client, config = appliance
    body = b"the same signed contract in two places"
    content_hash, blob_path = _blob(config, body)

    with factory() as session:
        doomed = _source(session, "Client D site")
        keeper = _source(session, "Client E site")
        doomed_object = _object(session, doomed, "/d/contract.pdf", content_hash)
        keeper_object = _object(session, keeper, "/e/contract.pdf", None)
        session.add(Blob(content_hash=content_hash, size_bytes=len(body), cached_path=str(blob_path)))
        document = Document(title="Contract")
        session.add(document)
        session.flush()
        version = DocumentVersion(document_id=document.id, content_hash=content_hash)
        session.add(version)
        session.flush()
        session.add(DocumentVersionSource(version_id=version.id, source_object_id=doomed_object.id))
        session.add(DocumentVersionSource(version_id=version.id, source_object_id=keeper_object.id))
        session.commit()
        doomed_id = doomed.id

    client.delete(f"/api/sources/{doomed_id}", headers=ADMIN_HEADERS).raise_for_status()

    assert blob_path.exists(), "the other source still reaches this version"
    with factory() as session:
        blob = session.get(Blob, content_hash)
        assert blob is not None and blob.cached_path == str(blob_path)


def test_a_firms_own_mounted_folder_is_never_deleted(appliance, factory, tmp_path):
    """Disconnecting a mounted share must not delete the share."""
    client, config = appliance
    mount = tmp_path / "firm-fileserver" / "Mandate"
    mount.mkdir(parents=True)
    (mount / "Vertrag.txt").write_text("Inhalt", encoding="utf-8")

    with factory() as session:
        source = _source(session, "Mounted share", root=str(mount))
        session.commit()
        source_id = source.id

    client.delete(f"/api/sources/{source_id}", headers=ADMIN_HEADERS).raise_for_status()
    assert (mount / "Vertrag.txt").read_text(encoding="utf-8") == "Inhalt"


def test_a_browser_import_the_appliance_made_is_reclaimed(appliance, factory, tmp_path):
    client, config = appliance
    imported = config.artifact_dir.parent / "browser-sources" / "deadbeef"
    imported.mkdir(parents=True)
    (imported / "Vertrag.pdf").write_bytes(b"uploaded by the operator")

    with factory() as session:
        source = _source(session, "Imported folder", root=str(imported))
        session.commit()
        source_id = source.id

    response = client.delete(f"/api/sources/{source_id}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["storage"]["files_removed"] == 1
    assert not imported.exists()


def test_a_browser_import_two_sources_share_survives_the_first_delete(appliance, factory):
    client, config = appliance
    imported = config.artifact_dir.parent / "browser-sources" / "cafebabe"
    imported.mkdir(parents=True)
    (imported / "Vertrag.pdf").write_bytes(b"uploaded once, connected twice")

    with factory() as session:
        first = _source(session, "First view", root=str(imported))
        _source(session, "Second view", root=str(imported))
        session.commit()
        first_id = first.id

    client.delete(f"/api/sources/{first_id}", headers=ADMIN_HEADERS).raise_for_status()
    assert (imported / "Vertrag.pdf").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can unlink from a read-only directory")
def test_a_file_that_will_not_go_is_reported_rather_than_hidden(appliance, factory, tmp_path):
    """The database saying 'gone' while bytes remain is the failure mode to surface."""
    client, config = appliance
    with factory() as session:
        source = _source(session, "Stubborn site")
        session.commit()
        source_id = source.id

    staged = _stage(config.artifact_dir, source_id, "Vertrag.pdf", b"cannot be unlinked")
    locked_dir = staged.parent
    original_mode = locked_dir.stat().st_mode
    locked_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        response = client.delete(f"/api/sources/{source_id}", headers=ADMIN_HEADERS)
        assert response.status_code == 200, response.text
        storage = response.json()["storage"]
        assert storage["complete"] is False
        assert storage["failures"], "a partial reclaim must name what stayed"
        assert str(staged.parent.parent.parent.parent) in storage["failures"][0] or str(
            locked_dir
        ) in " ".join(storage["failures"])
        assert staged.exists()
    finally:
        locked_dir.chmod(original_mode)


def test_deleting_a_source_with_no_content_reports_an_empty_reclaim(appliance, factory):
    client, _ = appliance
    with factory() as session:
        source = _source(session, "Never synced")
        session.commit()
        source_id = source.id

    storage = client.delete(f"/api/sources/{source_id}", headers=ADMIN_HEADERS).json()["storage"]
    assert storage == {
        "files_removed": 0,
        "bytes_reclaimed": 0,
        "blobs_removed": 0,
        "blobs_retained_shared": 0,
        "complete": True,
        "failures": [],
    }


def test_tombstoned_objects_of_another_source_still_hold_a_blob(appliance, factory):
    """A tombstone is a restorable observation, not an absent one."""
    client, config = appliance
    body = b"deleted at source, still known to us"
    content_hash, blob_path = _blob(config, body)

    with factory() as session:
        doomed = _source(session, "Client F site")
        keeper = _source(session, "Client G site")
        _object(session, doomed, "/f/doc.pdf", content_hash)
        tombstoned = _object(session, keeper, "/g/doc.pdf", content_hash)
        tombstoned.deleted_at = datetime.now(UTC)
        session.add(Blob(content_hash=content_hash, size_bytes=len(body), cached_path=str(blob_path)))
        session.commit()
        doomed_id = doomed.id

    client.delete(f"/api/sources/{doomed_id}", headers=ADMIN_HEADERS).raise_for_status()
    assert blob_path.exists()
