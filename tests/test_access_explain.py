"""``GET /api/access/explain`` — the evidence behind one principal's verdict.

The failure this guards against is a diagnosis that disagrees with the compiler: an
administrator told "she can see it" about a document retrieval will not return, or an
unhelpful "no match" where the real answer is a named group she is not in.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.connectors.principals import replace_memberships
from knowledge_index.db.models import (
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Source,
    SourceObject,
)
from knowledge_index.permissions import replace_source_object_grants
from knowledge_index.web.app import create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}
LITIGATION = "group:entra:de11dc47"
CORPORATE = "group:entra:df00ba1b"
INSIDER = "user:lit.user@firm.example"
OUTSIDER = "user:ursula@firm.example"


def _mirrored_estate(session: Session) -> dict[str, str]:
    """One SharePoint-shaped source: two documents, two groups, one member each."""
    source = Source(kind="sharepoint_online", display_name="SharePoint Online", provider="native")
    session.add(source)
    session.flush()
    replace_memberships(
        session,
        source.id,
        [
            {"group_id": "entra:de11dc47", "member_id": "lit.user@firm.example"},
            {"group_id": "entra:df00ba1b", "member_id": "corp.user@firm.example"},
        ],
    )
    ids: dict[str, str] = {}
    for index, (title, holder) in enumerate(
        (("Pretrial Brief", LITIGATION), ("Engagement Letter", CORPORATE))
    ):
        blob = Blob(content_hash=str(index) * 64, size_bytes=4)
        source_object = SourceObject(
            source_id=source.id, external_id=f"o-{index}", path=f"site/{title}.docx", name=title
        )
        document = Document(title=title)
        session.add_all([blob, source_object, document])
        session.flush()
        version = DocumentVersion(
            document_id=document.id, content_hash=blob.content_hash, ordinal=1
        )
        session.add(version)
        session.flush()
        session.add(DocumentVersionSource(version_id=version.id, source_object_id=source_object.id))
        replace_source_object_grants(
            session,
            source_object.id,
            [{"principal": holder, "effect": "allow", "origin": "manual"}],
        )
        ids[title] = document.id
    session.commit()
    return ids


def _client(factory: sessionmaker[Session], tmp_path: Path) -> TestClient:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    return TestClient(create_app(factory, store))


def test_explain_names_the_group_that_opened_each_document(
    factory: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    ids = _mirrored_estate(session)
    with _client(factory, tmp_path) as client:
        body = client.get(
            "/api/access/explain", params={"principal": INSIDER}, headers=ADMIN_HEADERS
        ).json()

    assert body["documents"]["visible"] == 1
    assert LITIGATION in body["resolved"]
    group = next(item for item in body["groups"] if item["principal"] == LITIGATION)
    # Entra reports the group by object id, so the mirrored member list is the only thing
    # that identifies it — inventing a display name here would be a lie.
    assert group["label"] is None
    assert group["members"] == ["lit.user@firm.example"]
    assert group["source"] == "SharePoint Online"

    brief = next(item for item in body["documents"]["items"] if item["id"] == ids["Pretrial Brief"])
    assert brief["visible"] is True
    assert brief["allowed_by"] == [{"scope": "source", "principal": LITIGATION}]

    letter = next(
        item for item in body["documents"]["items"] if item["id"] == ids["Engagement Letter"]
    )
    assert letter["visible"] is False
    # The point of the panel: a blocked document names the membership that would fix it.
    assert letter["source_allows"] == [{"principal": CORPORATE, "members": 1}]


def test_explain_reports_nothing_for_a_principal_nobody_mirrored(
    factory: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    _mirrored_estate(session)
    with _client(factory, tmp_path) as client:
        body = client.get(
            "/api/access/explain", params={"principal": OUTSIDER}, headers=ADMIN_HEADERS
        ).json()

    assert body["documents"]["visible"] == 0
    assert body["groups"] == []
    assert body["local_grants"] == []
    assert all(item["allowed_by"] == [] for item in body["documents"]["items"])


def test_a_deny_on_a_projectless_document_walls_it_off_and_says_so(
    factory: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """Connectors produce documents no project owns; those still need an ethical wall."""
    ids = _mirrored_estate(session)
    with _client(factory, tmp_path) as client:
        created = client.post(
            f"/api/documents/{ids['Pretrial Brief']}/grants",
            headers=ADMIN_HEADERS,
            json={
                "principal": INSIDER,
                "principal_kind": "user",
                "effect": "deny",
                "role": "viewer",
            },
        )
        assert created.status_code == 201
        body = client.get(
            "/api/access/explain", params={"principal": INSIDER}, headers=ADMIN_HEADERS
        ).json()

    assert body["documents"]["visible"] == 0
    brief = next(item for item in body["documents"]["items"] if item["id"] == ids["Pretrial Brief"])
    assert brief["visible"] is False
    assert brief["denied_by"][0]["scope"] == "document"
    # The source still allows it — the wall is local, and the panel has to show both.
    assert brief["allowed_by"] == [{"scope": "source", "principal": LITIGATION}]


def test_explain_requires_an_administrator(
    factory: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    _mirrored_estate(session)
    with _client(factory, tmp_path) as client:
        response = client.get(
            "/api/access/explain",
            params={"principal": INSIDER},
            headers={"x-ki-principals": INSIDER},
        )
    assert response.status_code == 403
