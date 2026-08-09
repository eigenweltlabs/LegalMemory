from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from fastmcp import Client
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import (
    AuditEvent,
    Blob,
    Chunk,
    CommunicationThread,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    EvalRecord,
    Matter,
    ProcessingState,
    Relation,
    Source,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.mcp_server import create_mcp_server, principals_from_headers
from knowledge_index.sync import deletions, runs as sync_runs
from knowledge_index.taxonomies import PIPELINE_STAGE_ORDER
from knowledge_index.web.app import create_app
from tests.conftest import TEST_EMBEDDING_MODEL

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}


def empty_app(
    factory: sessionmaker[Session], tmp_path: Path, *, orchestrator: str | None = None
) -> tuple[TestClient, ConfigStore]:
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    # These tests drive MCP tools by header rather than by OAuth handshake, which is the
    # development escape hatch and has to be asked for. The secure default is covered by
    # test_mcp_oauth.py.
    config.security.mcp_allow_trusted_header = True
    if orchestrator is not None:
        # The single-VM orchestrator, so a test that actually runs work needs no worker.
        config.components.orchestrator_provider = orchestrator
    store.save(config)
    return TestClient(create_app(factory, store)), store


def test_admin_ui_status_and_config_round_trip(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, store = empty_app(factory, tmp_path)
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Knowledge Index" in page.text
        assert client.get("/api/status", headers=ADMIN_HEADERS).json()["counts"]["documents"] == 0

        config = client.get("/api/config", headers=ADMIN_HEADERS).json()
        config["retrieval"]["rerank_enabled"] = True
        config["pipeline"]["stages"]["relate"]["model"] = "qwen-relate"
        response = client.put("/api/config", json=config, headers=ADMIN_HEADERS)
        assert response.status_code == 200
        assert store.get().retrieval.rerank_enabled is True
        assert store.get().pipeline.stage("relate").model == "qwen-relate"


def test_local_folder_connector_add_sync_and_remove(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path, orchestrator="local")
    root = tmp_path / "matters"
    (root / "Mandate").mkdir(parents=True)
    (root / "Mandate" / "NDA_final.txt").write_text("Vertraulichkeit", encoding="utf-8")

    with client:
        created = client.post(
            "/api/sources",
            json={
                "display_name": "Local matters",
                "kind": "local_fs",
                "provider": "native",
                "root": str(root),
                "default_acl": [
                    {"principal": "group:demo", "principal_kind": "group", "access": "allow"}
                ],
                "sync_policy": {"mode": "continuous", "interval": "5m"},
            },
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 201
        source_id = created.json()["id"]

        # The local_fs source is offered from the catalog alongside the API connectors.
        catalog = client.get("/api/connectors/catalog", headers=ADMIN_HEADERS).json()
        assert catalog[0]["id"] == "local_fs" and catalog[0]["provider"] == "native"

        queued = client.post("/api/actions/sync", headers=ADMIN_HEADERS)
        assert queued.status_code == 202
        enqueued = queued.json()
        assert [run["source_id"] for run in enqueued["runs"]] == [source_id]
        assert enqueued["skipped"] == []
        run_id = enqueued["runs"][0]["run_id"]

        # The endpoint reserved the work; the run itself finishes off the request path.
        sync_runs.wait_for_local_runs(timeout=60)
        run = next(
            row
            for row in client.get("/api/runs", headers=ADMIN_HEADERS).json()
            if row["id"] == run_id
        )
        assert run["workflow"] == "source-sync"
        assert run["status"] == "completed"
        assert run["counters"]["created"] == 1
        listed = {s["id"]: s for s in client.get("/api/sources", headers=ADMIN_HEADERS).json()}
        assert listed[source_id]["object_count"] == 1

        # A connection that is confirming a large deletion must still be disconnectable:
        # the held set references the source, and an administrator who cannot remove a
        # connection because its documents looked deleted is stuck with no way out.
        with factory() as session:
            deletions.record(
                session,
                source_id,
                {"Mandate/NDA_final.txt"},
                required=3,
                indexed_count=1,
            )
            session.commit()
        assert client.get("/api/sources", headers=ADMIN_HEADERS).json()[0]["pending_deletion"][
            "object_count"
        ] == 1

        removed = client.delete(f"/api/sources/{source_id}", headers=ADMIN_HEADERS)
        assert removed.status_code == 200
        assert removed.json()["removed_objects"] == 1
        assert source_id not in {
            s["id"] for s in client.get("/api/sources", headers=ADMIN_HEADERS).json()
        }
        # Deleting a missing source is a clean 404, not a 500.
        assert client.delete(f"/api/sources/{source_id}", headers=ADMIN_HEADERS).status_code == 404


def test_source_pipeline_counts_split_searchable_from_owed_work(
    factory: sessionmaker[Session],
) -> None:
    """Per-connection indexing truth for the connections page.

    ``indexed_count`` is what this connection can answer searches from;
    ``pending_pipeline_count`` is what the shared insertion pipeline still owes it.
    Terminal states — quarantined, disabled by configuration, tombstoned — are neither:
    counting them as owed work would pin an "indexing" spinner on a connection nothing
    will ever process.
    """
    from datetime import UTC, datetime

    from knowledge_index.db.models import ProcessingState, Source, SourceObject
    from knowledge_index.taxonomies import (
        DISABLED_BY_CONFIGURATION,
        WAITING_FOR_PREVIOUS_STAGE,
    )
    from knowledge_index.web.app import _source_pipeline_counts

    with factory() as session:
        source = Source(kind="local_fs", display_name="Mandate", config={})
        session.add(source)
        session.flush()

        def add_object(external_id: str, status: str, *, reason=None, deleted=False) -> None:
            row = SourceObject(
                source_id=source.id,
                external_id=external_id,
                path=external_id,
                name=external_id,
                deleted_at=datetime.now(UTC) if deleted else None,
            )
            session.add(row)
            session.flush()
            session.add(
                ProcessingState(
                    source_object_id=row.id,
                    stage="index",
                    status=status,
                    last_error={"reason": reason} if reason else None,
                )
            )

        add_object("indexed-1", "done")
        add_object("indexed-2", "done")
        add_object("still-converting", "skipped", reason=WAITING_FOR_PREVIOUS_STAGE)
        add_object("queued", "pending")
        add_object("poison", "quarantined")
        add_object("switched-off", "skipped", reason=DISABLED_BY_CONFIGURATION)
        add_object("tombstoned", "pending", deleted=True)
        session.flush()

        assert _source_pipeline_counts(session, source.id) == {
            "indexed_count": 2,
            "pending_pipeline_count": 2,
        }


def test_index_status_and_reindex_switch_to_model_bound_index(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.components.orchestrator_provider = "local"  # avoid needing a hatchet worker
    store.save(config)
    client = TestClient(create_app(factory, store))
    with client:
        status = client.get("/api/index/status", headers=ADMIN_HEADERS).json()
        assert status["chunk_count"] == 0
        assert status["locked"] is False
        assert status["derived_index_name"] == f"knowledge-index-chunks-{TEST_EMBEDDING_MODEL}-1536"

        # Catalog degrades gracefully when the gateway is unreachable in the test env.
        catalog = client.get("/api/models/catalog", headers=ADMIN_HEADERS).json()
        assert "models" in catalog

        result = client.post("/api/actions/reindex", headers=ADMIN_HEADERS)
        assert result.status_code == 200
        body = result.json()
        assert body["target_index"] == f"knowledge-index-chunks-{TEST_EMBEDDING_MODEL}-1536"
        assert body["chunks_to_reembed"] == 0
        # The persisted config now targets the model-bound index.
        assert store.get().retrieval.index_name == body["target_index"]


def test_principals_enumerates_grants_for_autocomplete(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    with client:
        project = client.post(
            "/api/projects",
            json={"key": "M-1", "name": "Matter one", "initial_principal": "group:ma-team"},
            headers=ADMIN_HEADERS,
        )
        assert project.status_code == 201
        project_id = project.json()["id"]
        client.post(
            f"/api/projects/{project_id}/grants",
            json={"principal": "user:alice", "principal_kind": "user", "role": "editor"},
            headers=ADMIN_HEADERS,
        )
        principals = client.get("/api/principals", headers=ADMIN_HEADERS).json()
        by_name = {item["principal"]: item for item in principals}
        assert "user:alice" in by_name
        assert by_name["user:alice"]["principal_kind"] == "user"
        assert "project" in by_name["user:alice"]["origins"]


def test_environment_review_approve_and_reject(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    with factory() as session:
        session.add_all(
            [
                EvalRecord(
                    id="env-1",
                    task_type="contract_drafting",
                    instruction="Draft the NDA for Projekt Falke.",
                    input_refs=[],
                    rubric=[{"id": "r1", "criterion": "Confidentiality clause", "weight": 0.5, "kind": "binary"}],
                    status="proposed",
                    authored_internally=True,
                    practice_area="corporate_ma",
                ),
                EvalRecord(
                    id="env-2",
                    task_type="legal_research",
                    instruction="Research limitation periods.",
                    input_refs=[],
                    rubric=[{"id": "r1", "criterion": "Cites the right statute", "weight": 0.5, "kind": "binary"}],
                    status="proposed",
                    authored_internally=True,
                ),
            ]
        )
        session.commit()

    with client:
        proposed = client.get("/api/environments?status=proposed", headers=ADMIN_HEADERS).json()
        assert {env["id"] for env in proposed} == {"env-1", "env-2"}
        assert proposed[0]["rubric"]  # rubric round-trips

        # An admin edits the candidate to make its criteria verifiable and gold-anchored.
        edited = client.patch(
            "/api/environments/env-1",
            json={
                "instruction": "Draft the NDA capping liability at the engagement fee.",
                "rubric": [
                    {
                        "criterion": "Cites the engagement fee cap",
                        "description": "States the exact cap amount",
                        "weight": 0.6,
                        "kind": "binary",
                        "essential": True,
                        "verifiable": True,
                        "gold_evidence": "§ 7.1: Haftung auf das Honorar begrenzt",
                    }
                ],
                "verifiers": [{"check": "cap_present", "detail": "a numeric cap appears", "expected": "Honorar"}],
            },
            headers=ADMIN_HEADERS,
        ).json()
        assert edited["instruction"].endswith("engagement fee.")
        assert edited["rubric"][0]["verifiable"] is True
        assert edited["rubric"][0]["gold_evidence"].startswith("§ 7.1")
        assert edited["verifiers"][0]["expected"] == "Honorar"
        # An invalid weight is rejected, not silently stored.
        assert (
            client.patch(
                "/api/environments/env-1",
                json={"rubric": [{"criterion": "x", "description": "y", "weight": 5, "kind": "binary"}]},
                headers=ADMIN_HEADERS,
            ).status_code
            == 422
        )

        approved = client.post("/api/environments/env-1/approve", headers=ADMIN_HEADERS).json()
        assert approved["status"] == "approved"
        assert approved["approved_by"]

        client.post("/api/environments/env-2/reject", headers=ADMIN_HEADERS)
        live = client.get("/api/environments?status=approved", headers=ADMIN_HEADERS).json()
        assert [env["id"] for env in live] == ["env-1"]
        assert client.post("/api/environments/missing/approve", headers=ADMIN_HEADERS).status_code == 404


def test_fs_list_browses_directories_for_the_folder_picker(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    (tmp_path / "Matters").mkdir()
    (tmp_path / "Archive").mkdir()
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    with client:
        listing = client.get(f"/api/fs/list?path={tmp_path}", headers=ADMIN_HEADERS).json()
        assert listing["path"] == str(tmp_path.resolve())
        assert set(listing["dirs"]) == {"Archive", "Matters"}  # dirs only, not note.txt
        assert listing["parent"] == str(tmp_path.parent)
        # a non-directory is a clean 404
        assert client.get(f"/api/fs/list?path={tmp_path}/note.txt", headers=ADMIN_HEADERS).status_code == 404
        # browsing requires admin
        assert client.get("/api/fs/list", headers={"x-ki-principals": "user:member"}).status_code in (401, 403)


def test_browser_folder_import_preserves_relative_paths_and_is_admin_only(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    selected = [
        ("files", ("contract.docx", b"contract bytes", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ("files", ("mail.eml", b"Subject: Test\n\nBody", "message/rfc822")),
    ]
    relative_paths = ["Test DMS/Matter A/contract.docx", "Test DMS/Matter B/mail.eml"]
    with client:
        denied = client.post(
            "/api/fs/import-folder",
            files=selected,
            data={"relative_paths": json.dumps(relative_paths)},
            headers={"x-ki-principals": "user:member"},
        )
        assert denied.status_code in (401, 403)

        imported = client.post(
            "/api/fs/import-folder",
            files=selected,
            data={"relative_paths": json.dumps(relative_paths)},
            headers=ADMIN_HEADERS,
        )
        assert imported.status_code == 201
        payload = imported.json()
        root = Path(payload["root"])
        assert root.is_relative_to(tmp_path)
        assert payload["folder_name"] == "Test DMS"
        assert payload["file_count"] == 2
        assert (root / "Matter A" / "contract.docx").read_bytes() == b"contract bytes"
        assert (root / "Matter B" / "mail.eml").read_bytes().endswith(b"Body")

        unsafe = client.post(
            "/api/fs/import-folder",
            files=[("files", ("escape.txt", b"no", "text/plain"))],
            data={"relative_paths": json.dumps(["Test DMS/../escape.txt"])},
            headers=ADMIN_HEADERS,
        )
        assert unsafe.status_code == 422


def test_add_source_requires_admin(factory: sessionmaker[Session], tmp_path: Path) -> None:
    client, _ = empty_app(factory, tmp_path)
    with client:
        response = client.post(
            "/api/sources",
            json={"display_name": "x", "kind": "local_fs", "root": str(tmp_path)},
            headers={"x-ki-principals": "user:member"},
        )
        assert response.status_code in (401, 403)


def test_search_requires_trusted_principal_header(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    with client:
        response = client.post("/api/search", json={"query": "Haftung"})
        assert response.status_code == 401


def test_mcp_http_mount_initializes_with_parent_lifespan(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    with client:
        response = client.post(
            "/mcp/",
            json=request,
            # `initialize` is behind the same gate as every other MCP method: the
            # transport is what is protected, not the individual tools.
            headers={"accept": "application/json, text/event-stream", **ADMIN_HEADERS},
        )
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "Knowledge Index"


def test_mcp_tool_call_requires_identity_and_writes_audit_event(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "list_taxonomies", "arguments": {}},
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "x-ki-principals": "user:demo,group:ma-team",
    }
    with client:
        response = client.post("/mcp/", json=request, headers=headers)
        denied = client.post(
            "/mcp/",
            json={**request, "id": 3},
            headers={"accept": "application/json, text/event-stream"},
        )
        failed = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_filter",
                    "arguments": {"date_from": "not-an-iso-date"},
                },
            },
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    # An identityless call is refused by the transport rather than answered with a
    # JSON-RPC error, so the client sees a challenge it can act on.
    assert denied.status_code == 401
    assert "resource_metadata=" in denied.headers["www-authenticate"]
    assert failed.status_code == 200
    assert failed.json()["result"]["isError"] is True

    factory = client.app.state.session_factory
    with factory() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "mcp.list_taxonomies")
            .order_by(AuditEvent.outcome)
        ).all()
        assert [(event.outcome, event.actor_principals) for event in events] == [
            ("success", ["group:ma-team", "role:authenticated", "user:demo"]),
        ]
        failed_event = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "mcp.search_filter")
        )
        assert failed_event is not None
        assert failed_event.outcome == "error"
        assert failed_event.details["error_class"] == "ValueError"


def test_mcp_exposes_granular_tools(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.security.mcp_allow_trusted_header = True
    mcp = create_mcp_server(factory, lambda: config)

    async def names() -> set[str]:
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    assert asyncio.run(names()) == {
        "billing_rollup",
        "download_document",
        "find_related_documents",
        "get_document",
        "list_firm_people",
        "list_invoices",
        "list_matters",
        "list_taxonomies",
        "ontology_children",
        "ontology_node",
        "ontology_roots",
        "ontology_search",
        "preview_search_scope",
        "resolve_entity",
        "search_decisions",
        "search_filter",
        "search_semantic",
        "traverse",
    }
    assert principals_from_headers({"x-ki-principals": "user:a, group:ma"}, config) == {
        "user:a",
        "group:ma",
        "role:authenticated",
    }


def test_data_explorer_lists_complete_filtered_pages_and_full_graph(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, _ = empty_app(factory, tmp_path)
    with factory() as session:
        source = Source(id="viewer-source", kind="local_fs", display_name="Viewer corpus")
        matter = Matter(id="viewer-matter", title="Complete viewer matter")
        session.add_all([source, matter])
        session.flush()
        for index, (status, language, doc_type) in enumerate(
            [
                ("final", "en", "other_contract"),
                ("final", "en", "letter"),
                ("draft", "de", "internal_note"),
            ],
            start=1,
        ):
            content_hash = f"{index:064x}"
            document_id = f"viewer-document-{index}"
            version_id = f"viewer-version-{index}"
            source_object_id = f"viewer-source-object-{index}"
            session.add_all(
                [
                    Blob(
                        content_hash=content_hash,
                        size_bytes=index * 10,
                        mime_sniffed="text/plain",
                    ),
                    SourceObject(
                        id=source_object_id,
                        source_id=source.id,
                        external_id=f"matter/file-{index}.txt",
                        path=f"matter/file-{index}.txt",
                        name=f"file-{index}.txt",
                        mime_type="text/plain",
                        content_hash=content_hash,
                    ),
                    Document(
                        id=document_id,
                        matter_id=matter.id,
                        title=f"Viewer document {index}",
                        doc_type=doc_type,
                        language=language,
                    ),
                ]
            )
            session.flush()
            session.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    content_hash=content_hash,
                    ordinal=1,
                    status=status,
                )
            )
            session.flush()
            session.add_all(
                [
                    DocumentVersionSource(
                        version_id=version_id,
                        source_object_id=source_object_id,
                    ),
                    Chunk(
                        id=f"viewer-chunk-{index}",
                        document_version_id=version_id,
                        document_id=document_id,
                        matter_id=matter.id,
                        ordinal=0,
                        text=f"Complete viewer text {index}",
                    ),
                ]
            )
        thread = CommunicationThread(
            id="viewer-thread",
            matter_id=matter.id,
            subject_norm="Viewer correspondence",
            participants=["alice@example.com", "bob@example.com"],
        )
        session.add(thread)
        session.add_all(
            [
                Relation(
                    id="viewer-thread-edge",
                    from_type="document",
                    from_id="viewer-document-1",
                    to_type="thread",
                    to_id=thread.id,
                    kind="belongs_to_thread",
                    provenance={"method": "test"},
                ),
                Relation(
                    id="viewer-version-edge",
                    from_type="document_version",
                    from_id="viewer-version-2",
                    to_type="document_version",
                    to_id="viewer-version-1",
                    kind="supersedes",
                    provenance={"method": "test"},
                ),
            ]
        )
        session.commit()

    with client:
        response = client.get(
            "/api/documents",
            params={
                "detailed": "true",
                "matter_id": "viewer-matter",
                "version_status": "final",
                "language": "en",
                "limit": 1,
                "offset": 1,
            },
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 200
        ledger = response.json()
        assert ledger["pagination"] == {
            "total": 2,
            "offset": 1,
            "limit": 1,
            "returned": 1,
            "has_more": False,
        }
        assert ledger["items"][0]["chunks"] == 1
        assert ledger["items"][0]["matter"]["title"] == "Complete viewer matter"
        assert {facet["value"] for facet in ledger["facets"]["doc_types"]} == {
            "letter",
            "other_contract",
        }

        graph = client.get("/api/graph", headers=ADMIN_HEADERS).json()
        assert graph["summary"]["documents"] == 3
        assert graph["summary"]["total_documents"] == 3
        assert graph["summary"]["truncated"] is False
        assert graph["summary"]["by_kind"] == {
            "document": 3,
            "matter": 1,
            "source": 1,
            "source_object": 3,
            "thread": 1,
            "version": 3,
        }
        assert graph["summary"]["by_edge_kind"]["belongs_to_thread"] == 1
        assert graph["summary"]["by_edge_kind"]["supersedes"] == 1
        assert graph["summary"]["by_edge_kind"]["observed_as"] == 3


def test_admin_status_and_data_viewer_exclude_tombstones(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Admin bypasses ACLs, not source lifecycle.

    Tombstones retain their versions and chunks so a restored provider object reconnects
    without losing audit history. They must not inflate the live estate on the dashboard
    or appear as ordinary documents in the Data Viewer.
    """

    client, _ = empty_app(factory, tmp_path)
    with factory() as session:
        source = Source(id="lifecycle-source", kind="local_fs", display_name="Lifecycle corpus")
        session.add(source)
        session.flush()
        for index, deleted in ((1, False), (2, True)):
            content_hash = f"{index + 100:064x}"
            document_id = f"lifecycle-document-{index}"
            version_id = f"lifecycle-version-{index}"
            source_object_id = f"lifecycle-object-{index}"
            session.add_all(
                [
                    Blob(
                        content_hash=content_hash,
                        size_bytes=index,
                        mime_sniffed="text/plain",
                    ),
                    SourceObject(
                        id=source_object_id,
                        source_id=source.id,
                        external_id=f"file-{index}.txt",
                        path=f"file-{index}.txt",
                        name=f"file-{index}.txt",
                        mime_type="text/plain",
                        content_hash=content_hash,
                        deleted_at=datetime.now(UTC) if deleted else None,
                    ),
                    Document(
                        id=document_id,
                        title=f"Lifecycle document {index}",
                    ),
                ]
            )
            session.flush()
            session.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    content_hash=content_hash,
                    ordinal=1,
                    status="final",
                )
            )
            session.flush()
            session.add_all(
                [
                    DocumentVersionSource(
                        version_id=version_id,
                        source_object_id=source_object_id,
                    ),
                    Chunk(
                        id=f"lifecycle-chunk-{index}",
                        document_version_id=version_id,
                        document_id=document_id,
                        ordinal=0,
                        text=f"Lifecycle text {index}",
                    ),
                ]
            )
        session.commit()

    with client:
        counts = client.get("/api/status", headers=ADMIN_HEADERS).json()["counts"]
        assert counts["source_objects"] == 1
        assert counts["documents"] == 1
        assert counts["chunks"] == 1

        ledger = client.get(
            "/api/documents",
            params={"detailed": "true"},
            headers=ADMIN_HEADERS,
        ).json()
        assert ledger["pagination"]["total"] == 1
        assert [item["id"] for item in ledger["items"]] == ["lifecycle-document-1"]
        assert (
            client.get(
                "/api/documents/lifecycle-document-2",
                headers=ADMIN_HEADERS,
            ).status_code
            == 404
        )


def _settled_object(
    session: Session,
    source: Source,
    object_id: str,
    *,
    quarantined: str | None = None,
    handler_skips: dict[str, str] | None = None,
) -> None:
    """One source object with a full set of settled ``processing_state`` rows."""
    session.add(
        SourceObject(
            id=object_id,
            source_id=source.id,
            external_id=f"matter/{object_id}.txt",
            path=f"matter/{object_id}.txt",
            name=f"{object_id}.txt",
            mime_type="text/plain",
            content_hash=f"{abs(hash(object_id)) % (16**64):064x}",
        )
    )
    session.flush()
    stages = [item.value for item in PIPELINE_STAGE_ORDER]
    parked = stages.index(quarantined) if quarantined else len(stages)
    for position, stage in enumerate(stages):
        if position < parked:
            reason = (handler_skips or {}).get(stage)
            status = "skipped" if reason else "done"
            error = {"reason": reason} if reason else None
            attempts = 1
        elif position == parked:
            status = "quarantined"
            error = {
                "class": "ConnectError",
                "message": "model gateway unreachable",
                "deterministic": False,
            }
            attempts = 3
        else:
            status, error, attempts = "skipped", {"reason": "waiting_for_previous_stage"}, 0
        session.add(
            ProcessingState(
                source_object_id=object_id,
                stage=stage,
                status=status,
                attempts=attempts,
                last_error=error,
                producer_version="mvp-1" if position <= parked and status != "quarantined" else None,
            )
        )


def test_quarantined_document_is_released_retried_and_audited(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Quarantine used to be terminal: no version bump reclaims a row that is neither
    done nor skipped, so one failure against a service that happened to be down took a
    document out of the index permanently."""
    client, store = empty_app(factory, tmp_path, orchestrator="local")
    # Every stage switched off, so the run the retry starts settles without a model
    # gateway. What is under test is that the parked row became claimable at all.
    config = store.get()
    for name, stage in list(config.pipeline.stages.items()):
        config.pipeline.stages[name] = stage.model_copy(update={"enabled": False})
    store.save(config)

    with factory() as session:
        source = Source(id="parked-source", kind="local_fs", display_name="Parked corpus")
        session.add(source)
        session.flush()
        _settled_object(session, source, "parked-object", quarantined="convert")
        # A second file that reached the end, with one genuine handler skip on it: the
        # count that used to be reported as "skipped by configuration".
        _settled_object(
            session, source, "clean-object", handler_skips={"extract_decisions": "no_decisions"}
        )
        session.commit()

    with client:
        # A stage that is not quarantined is not silently "retried".
        assert (
            client.post(
                "/api/quarantine/parked-object/retry?stage=index", headers=ADMIN_HEADERS
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/quarantine/parked-object/retry",
                headers={"x-ki-principals": "user:reader"},
            ).status_code
            == 403
        ), "releasing a quarantined document is an administrator action"

        released = client.post("/api/quarantine/parked-object/retry", headers=ADMIN_HEADERS)
        assert released.status_code == 200
        body = released.json()
        assert body["stage"] == "convert"
        # Everything derived from the failed run is invalidated with it, exactly as a
        # producer-version bump does.
        assert body["invalidated_stages"] == [
            "classify_matter",
            "relate",
            "extract_metadata",
            "extract_decisions",
            "index",
        ]
        assert body["deterministic"] is False
        assert body["previous_error"]["class"] == "ConnectError"
        assert body["max_attempts"] == config.pipeline.stage("convert").max_attempts
        assert body["run"]["run_id"]

        assert client.get("/api/quarantine", headers=ADMIN_HEADERS).json() == []
        pipeline = client.get("/api/status", headers=ADMIN_HEADERS).json()["pipeline"]

    with factory() as session:
        convert = session.scalar(
            select(ProcessingState).where(
                ProcessingState.source_object_id == "parked-object",
                ProcessingState.stage == "convert",
            )
        )
        assert convert is not None
        # Claimed and executed again: the attempt counter was reset by the release and
        # then spent by the run.
        assert convert.status != "quarantined"
        assert convert.attempts == 1
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "quarantine.retry")
        )
        assert audit is not None
        assert audit.target_id == "parked-object"
        assert audit.details["stage"] == "convert"
        assert audit.details["previous_error"]["class"] == "ConnectError"

    # A stage that is off and a handler that declined a file are two different facts and
    # are counted separately. Reporting both as "skipped" is what made the enabled toggle
    # look inert: it showed handler skips as its own work.
    assert pipeline["convert"] == {"disabled": 1, "done": 1}
    assert pipeline["extract_decisions"] == {"disabled": 1, "skipped": 1}
    assert pipeline["fetch"] == {"done": 2}


def test_status_and_cli_agree_on_the_disabled_bucket() -> None:
    from knowledge_index.taxonomies import stage_bucket

    assert stage_bucket("skipped", {"reason": "disabled_by_configuration"}) == "disabled"
    assert stage_bucket("skipped", {"reason": "waiting_for_previous_stage"}) == "waiting"
    assert stage_bucket("skipped", {"reason": "no_decisions"}) == "skipped"
    assert stage_bucket("skipped", None) == "skipped"
    assert stage_bucket("done", None) == "done"


def test_external_console_reads_the_mcp_tool_list_from_the_server(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The console carried its own copy of this list and had drifted five tools behind,
    telling an administrator that connected clients could do less than they can."""
    client, _ = empty_app(factory, tmp_path)
    with client:
        assert client.get("/api/mcp/tools", headers={"x-ki-principals": "user:reader"}).status_code == 403
        listed = client.get("/api/mcp/tools", headers=ADMIN_HEADERS)
        assert listed.status_code == 200
        tools = listed.json()

    mcp = create_mcp_server(factory, lambda: AppConfig(artifact_dir=tmp_path / "artifacts"))

    async def registered() -> list[str]:
        async with Client(mcp) as connected:
            return [tool.name for tool in await connected.list_tools()]

    assert [tool["name"] for tool in tools] == asyncio.run(registered())
    assert len(tools) == 18  # 14 retrieval/scope tools + 4 ontology navigation tools
    for tool in tools:
        # One line per tool: the model-facing description is a paragraph and must not
        # reach a console whose copy is deliberately terse.
        assert tool["summary"] and "\n" not in tool["summary"] and len(tool["summary"]) <= 120
        assert tool["tags"]

    page = (Path(__file__).resolve().parents[1] / "ui" / "src" / "pages" / "ExternalPage.jsx").read_text()
    assert [tool["name"] for tool in tools if tool["name"] in page] == [], (
        "ExternalPage must render GET /api/mcp/tools, never a list of its own"
    )


def test_matter_lookup_finds_a_matter_by_its_own_name_within_the_caller_acl(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """``/api/graph?query=`` matches ``Document.title`` only, so the command palette found
    a matter solely when one of its files happened to be named after it."""
    client, _ = empty_app(factory, tmp_path)
    with factory() as session:
        source = Source(id="atlas-source", kind="sharepoint_online", display_name="Atlas SP")
        matter = Matter(
            id="atlas-matter", title="Atlas Industrial Acquisition", practice_area="corporate_ma"
        )
        session.add_all([source, matter])
        session.flush()
        content_hash = f"{42:064x}"
        session.add_all(
            [
                Blob(content_hash=content_hash, size_bytes=10, mime_sniffed="text/plain"),
                SourceObject(
                    id="atlas-object",
                    source_id=source.id,
                    external_id="atlas/spa.txt",
                    path="atlas/spa.txt",
                    name="spa.txt",
                    mime_type="text/plain",
                    content_hash=content_hash,
                ),
                # Deliberately not named after the matter: this is the case that used to
                # return nothing.
                Document(id="atlas-document", matter_id=matter.id, title="Kaufvertrag Entwurf"),
            ]
        )
        session.flush()
        session.add(
            DocumentVersion(
                id="atlas-version",
                document_id="atlas-document",
                content_hash=content_hash,
                ordinal=1,
                status="final",
            )
        )
        session.flush()
        session.add_all(
            [
                DocumentVersionSource(
                    version_id="atlas-version", source_object_id="atlas-object"
                ),
                SourceObjectGrant(
                    source_object_id="atlas-object",
                    principal="group:corporate",
                    principal_kind="group",
                    effect="allow",
                ),
            ]
        )
        session.commit()

    with client:
        found = client.get("/api/matters", params={"query": "Atlas Industrial"}, headers=ADMIN_HEADERS)
        assert found.status_code == 200
        assert found.json() == [
            {
                "id": "atlas-matter",
                "title": "Atlas Industrial Acquisition",
                "practice_area": "corporate_ma",
                "documents": 1,
            }
        ]
        # The old path, for the record: no document title contains the matter name.
        graph = client.get(
            "/api/graph", params={"query": "Atlas Industrial"}, headers=ADMIN_HEADERS
        ).json()
        assert [node for node in graph["nodes"] if node["kind"] == "matter"] == []

        # Scoped through the documents the caller can read, never through projects — this
        # deployment has none.
        member = {"x-ki-principals": "user:corp.user,group:corporate"}
        assert [row["id"] for row in client.get(
            "/api/matters", params={"query": "atlas"}, headers=member
        ).json()] == ["atlas-matter"]
        outsider = {"x-ki-principals": "user:outsider"}
        assert client.get("/api/matters", params={"query": "atlas"}, headers=outsider).json() == []
