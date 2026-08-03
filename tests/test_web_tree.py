"""The folder projection, and the permission boundary it has to keep.

`DocumentTreeService` re-states the source-object half of the permission
compiler as SQL, because a folder listing cannot afford the per-object Python
walk `RetrievalService._authorized_sources` does. Two implementations of one
rule is exactly the arrangement that drifts, and drift here is not a wrong
count: it is a lawyer reading the name of a file they may not open, which is
the disclosure the ethical wall exists to prevent.

So the tests that matter most below are the ones that assert absence.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import (
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    Source,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.web.app import create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}
URSULA_HEADERS = {"x-ki-principals": "user:ursula@firm.de"}


def _app(factory: sessionmaker[Session], tmp_path: Path) -> TestClient:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    return TestClient(create_app(factory, store))


def _seed(factory: sessionmaker[Session]) -> dict[str, str]:
    """One connector, three files, one of them behind a mirrored grant.

    Paths are deliberately relative and one of them Windows-shaped: both are
    what connectors actually report, and both have to land in the same tree.
    """

    ids: dict[str, str] = {}
    with factory() as session:
        source = Source(kind="sharepoint_online", display_name="SharePoint")
        session.add(source)
        session.flush()
        ids["source_id"] = source.id

        matter = Matter(title="Okafor-Reyes v. Creston")
        session.add(matter)
        session.flush()

        files = [
            ("matters/LIT-1/brief.docx", "brief.docx", None),
            ("matters\\LIT-1\\exhibit.pdf", "exhibit.pdf", None),
            # Only Ursula's colleague may see this one.
            ("matters/CORP-2/secret.docx", "secret.docx", "user:someone-else@firm.de"),
        ]
        for index, (path, name, restricted_to) in enumerate(files):
            source_object = SourceObject(
                source_id=source.id,
                external_id=f"ext-{index}",
                path=path,
                name=name,
                mime_type="application/octet-stream",
                size_bytes=100 + index,
                content_hash=f"hash-{index}",
            )
            document = Document(matter_id=matter.id, title=f"Document {index}")
            # A version points at the bytes it is a version of; the FK is what
            # keeps a document from citing content the appliance never held.
            session.add_all(
                [
                    Blob(content_hash=f"hash-{index}", size_bytes=100 + index),
                    source_object,
                    document,
                ]
            )
            session.flush()
            version = DocumentVersion(
                document_id=document.id,
                ordinal=1,
                status="final",
                content_hash=f"hash-{index}",
            )
            session.add(version)
            session.flush()
            session.add(
                DocumentVersionSource(
                    version_id=version.id, source_object_id=source_object.id
                )
            )
            # An external connector fails closed without a mirrored allow, so
            # every file needs one; `restricted_to` names somebody else.
            session.add(
                SourceObjectGrant(
                    source_object_id=source_object.id,
                    principal=restricted_to or "user:ursula@firm.de",
                    principal_kind="user",
                    effect="allow",
                )
            )
            ids[name] = document.id
        session.commit()
    return ids


def test_roots_count_only_what_the_caller_can_see(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed(factory)
    client = _app(factory, tmp_path)
    with client:
        admin = client.get("/api/tree/roots", headers=ADMIN_HEADERS).json()["roots"]
        assert [root["files"] for root in admin] == [3]

        # The same connector, a smaller number. Two lawyers seeing different
        # counts under one source is the ethical wall being visible rather than
        # merely enforced.
        ursula = client.get("/api/tree/roots", headers=URSULA_HEADERS).json()["roots"]
        assert [root["files"] for root in ursula] == [2]


def test_children_normalizes_separators_and_paginates(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    ids = _seed(factory)
    client = _app(factory, tmp_path)
    with client:
        source_id = client.get("/api/tree/roots", headers=ADMIN_HEADERS).json()["roots"][0][
            "source_id"
        ]

        root = client.get(
            "/api/tree/children",
            params={"source_id": source_id},
            headers=ADMIN_HEADERS,
        ).json()
        assert [folder["name"] for folder in root["folders"]] == ["matters"]
        assert root["folders"][0]["files"] == 3
        assert root["files"] == []

        # `matters\LIT-1\exhibit.pdf` and `matters/LIT-1/brief.docx` are one
        # folder, not two, which is the whole job of path normalization.
        level = client.get(
            "/api/tree/children",
            params={"source_id": source_id, "path": "/matters"},
            headers=ADMIN_HEADERS,
        ).json()
        assert [folder["name"] for folder in level["folders"]] == ["CORP-2", "LIT-1"]

        page = client.get(
            "/api/tree/children",
            params={"source_id": source_id, "path": "/matters/LIT-1", "limit": 1},
            headers=ADMIN_HEADERS,
        ).json()
        assert page["pagination"] == {
            "total": 2,
            "offset": 0,
            "limit": 1,
            "returned": 1,
            "has_more": True,
        }
        assert page["files"][0]["name"] == "brief.docx"
        assert page["files"][0]["document_id"] == ids["brief.docx"]

        rest = client.get(
            "/api/tree/children",
            params={"source_id": source_id, "path": "/matters/LIT-1", "limit": 1, "offset": 1},
            headers=ADMIN_HEADERS,
        ).json()
        assert [file["name"] for file in rest["files"]] == ["exhibit.pdf"]
        assert rest["pagination"]["has_more"] is False


def test_children_hides_the_folder_a_caller_may_not_read(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The path is the disclosure, so an unreadable file takes its folder with it."""

    _seed(factory)
    client = _app(factory, tmp_path)
    with client:
        source_id = client.get("/api/tree/roots", headers=URSULA_HEADERS).json()["roots"][0][
            "source_id"
        ]
        level = client.get(
            "/api/tree/children",
            params={"source_id": source_id, "path": "/matters"},
            headers=URSULA_HEADERS,
        ).json()
        # CORP-2 holds only the restricted file, so for Ursula it does not exist.
        assert [folder["name"] for folder in level["folders"]] == ["LIT-1"]
        assert level["folders"][0]["files"] == 2


def test_locate_returns_the_ancestors_and_the_page_the_file_is_on(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    ids = _seed(factory)
    client = _app(factory, tmp_path)
    with client:
        located = client.get(
            "/api/tree/locate",
            params={"document_id": ids["exhibit.pdf"]},
            headers=ADMIN_HEADERS,
        ).json()
        assert located["path"] == "/matters/LIT-1"
        assert located["ancestors"] == ["/matters", "/matters/LIT-1"]
        # Second of two under the ordering `children` pages by, so a client
        # asking for one row per page knows to fetch the second.
        assert located["index"] == 1
        assert located["file"]["name"] == "exhibit.pdf"


def test_locate_refuses_a_document_the_caller_cannot_read(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    ids = _seed(factory)
    client = _app(factory, tmp_path)
    with client:
        assert (
            client.get(
                "/api/tree/locate",
                params={"document_id": ids["secret.docx"]},
                headers=URSULA_HEADERS,
            ).status_code
            == 404
        )


def test_search_matches_filenames_within_the_caller_scope(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _seed(factory)
    client = _app(factory, tmp_path)
    with client:
        admin = client.get(
            "/api/tree/search", params={"query": "docx"}, headers=ADMIN_HEADERS
        ).json()["files"]
        assert sorted(file["name"] for file in admin) == ["brief.docx", "secret.docx"]

        ursula = client.get(
            "/api/tree/search", params={"query": "docx"}, headers=URSULA_HEADERS
        ).json()["files"]
        assert [file["name"] for file in ursula] == ["brief.docx"]


def test_search_does_not_treat_wildcards_as_syntax(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """`%` is a legal character in a filename and must not match everything."""

    _seed(factory)
    client = _app(factory, tmp_path)
    with client:
        assert (
            client.get(
                "/api/tree/search", params={"query": "%"}, headers=ADMIN_HEADERS
            ).json()["files"]
            == []
        )
