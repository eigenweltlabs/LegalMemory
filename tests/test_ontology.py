"""Ontology system: artifact loading, scoped navigation, fingerprints, filters.

Covers the pluggable-ontology design: LMSS as shipped artifact, deterministic
roots/children/node tools, on/off node scoping (no packs, no depth caps),
ancestor-closure subtree filters, the visited-node discipline of the metadata
agent's tools, the selective requeue on scope changes, and the web/MCP surface.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import (
    Blob,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    ProcessingState,
    Source,
    SourceObject,
)
from knowledge_index.ontology import discover_artifacts, ontology_scope
from knowledge_index.pipeline import PipelineRunner
from knowledge_index.pipeline.ontology_tools import (
    clause_search_tool,
    ontology_navigation_tools,
    service_navigation_tools,
)
from knowledge_index.search_backend import _combined_filter
from knowledge_index.retrieval_types import SearchFilters
from knowledge_index.taxonomies import PipelineStage, ProcessingStatus
from knowledge_index.web.app import create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}


def scope_for(**ontology_overrides):
    config = AppConfig.model_validate({"ontology": ontology_overrides})
    return config.doc_ontology()


# --- artifact + navigation --------------------------------------------------


def test_shipped_lmss_artifact_loads_with_doc_type_facet() -> None:
    scope = scope_for()
    assert scope.artifact.name == "lmss"
    assert len(scope.artifact.nodes) > 18_000
    root_labels = {root["label"] for root in scope.roots()}
    assert root_labels == {
        "Document Types",
        "Knowledge Type",
        "Written Asynchronous Communication",
    }
    # the doc_type facet is a small slice of the whole artifact
    assert 1_000 < len(scope.visible) < 2_000


def test_navigation_is_deterministic_and_scoped() -> None:
    scope = scope_for()
    [doc_types_root] = [r for r in scope.roots() if r["label"] == "Document Types"]
    first = scope.children(doc_types_root["id"])
    second = scope.children(doc_types_root["id"])
    assert first == second  # pure lookup, stable order
    assert scope.children("nonexistent-node") == []
    assert scope.node("nonexistent-node") is None
    detail = scope.node(first[0]["id"])
    assert detail["path"][0] == "Document Types"


def test_search_finds_nodes_by_label_and_is_ordered() -> None:
    scope = scope_for()
    hits = scope.search("intercreditor")
    assert hits and hits[0]["label"] == "Intercreditor Agreement"
    assert "Agreements" in hits[0]["path"]
    assert scope.search("") == []


def test_disabling_a_node_hides_its_subtree_and_changes_fingerprint() -> None:
    base = scope_for()
    [agreements] = [hit for hit in base.search("Agreements") if hit["label"] == "Agreements"]
    scoped = scope_for(disabled_nodes=[agreements["id"]])
    assert scoped.fingerprint != base.fingerprint
    assert agreements["id"] not in scoped.visible
    # a deep node under Agreements is hidden too, and resolves upward
    [ica] = [h for h in base.search("intercreditor") if h["label"] == "Intercreditor Agreement"]
    assert ica["id"] not in scoped.visible
    resolved = scoped.resolve(ica["id"])
    assert resolved in scoped.visible
    assert scoped.label_of(resolved) in {"Transactional Document", "Document Types"}
    # unknown ids resolve to None
    assert scoped.resolve("no-such-node") is None


def test_ancestor_closure_supports_subtree_filtering() -> None:
    scope = scope_for()
    [ica] = [h for h in scope.search("intercreditor") if h["label"] == "Intercreditor Agreement"]
    ancestors = scope.ancestors(ica["id"])
    assert ica["id"] in ancestors  # closure includes self
    labels = {scope.label_of(node) for node in ancestors}
    assert {"Agreements", "Transactional Document", "Document Types"} <= labels


def test_doc_type_filter_matches_against_ancestor_closure() -> None:
    class _AllowAll:
        def opensearch_filter(self) -> dict:
            return {"match_all": {}}

    query = _combined_filter(_AllowAll(), SearchFilters(doc_type="some-node"))
    clauses = query["bool"]["filter"]
    assert {"term": {"doc_type_ancestors": "some-node"}} in clauses


# --- agent tools: visited-node discipline -----------------------------------


def test_agent_tools_record_every_returned_node_as_visited() -> None:
    scope = scope_for()
    visited: set[str] = set()
    tools = {tool.name: tool for tool in ontology_navigation_tools(scope, visited)}
    tools["ontology_roots"].handler({})
    root_id = scope.roots()[0]["id"]
    assert root_id in visited
    tools["ontology_children"].handler({"node_id": root_id})
    assert len(visited) > 2
    # a node never returned by any tool is not visited
    [ica] = [h for h in scope.search("intercreditor") if h["label"] == "Intercreditor Agreement"]
    assert ica["id"] not in visited
    tools["ontology_node"].handler({"node_id": ica["id"]})
    assert ica["id"] in visited
    # unknown nodes error without polluting the visited set
    result = tools["ontology_children"].handler({"node_id": "bogus"})
    assert "error" in result and "bogus" not in visited
    # search results count as visited too — the agent may submit what search showed
    before = len(visited)
    tools["ontology_search"].handler({"query": "checklist"})
    assert len(visited) > before


# --- selective requeue on scope change --------------------------------------


def _seed_typed_document(
    session: Session,
    source: Source,
    *,
    node_id: str | None,
    marker: str,
    fingerprint: str | None = None,
) -> str:
    blob = Blob(content_hash=f"hash-{marker}", size_bytes=10)
    session.add(blob)
    source_object = SourceObject(
        source_id=source.id,
        external_id=f"ext-{marker}",
        path=f"Clients/X/{marker}.txt",
        name=f"{marker}.txt",
        content_hash=blob.content_hash,
    )
    session.add(source_object)
    session.flush()
    scope = AppConfig().doc_ontology()
    document = Document(
        doc_type=node_id,
        doc_type_ancestors=sorted(scope.ancestors(node_id)) if node_id else [],
        ontology_fingerprint=fingerprint or scope.fingerprint,
        title=marker,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(document_id=document.id, content_hash=blob.content_hash)
    session.add(version)
    session.flush()
    session.add(DocumentVersionSource(version_id=version.id, source_object_id=source_object.id))
    defaults = AppConfig().pipeline
    for stage in PipelineStage:
        session.add(
            ProcessingState(
                source_object_id=source_object.id,
                stage=stage.value,
                status=ProcessingStatus.DONE.value,
                producer_version=defaults.stage(stage.value).producer_version,
            )
        )
    return source_object.id


def test_scope_change_requeues_only_documents_on_hidden_nodes(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    base = AppConfig().doc_ontology()
    [ica] = [h for h in base.search("intercreditor") if h["label"] == "Intercreditor Agreement"]
    [litigation] = [
        h for h in base.search("Litigation Document") if h["label"] == "Litigation Document"
    ]
    with factory() as session:
        source = Source(kind="local_fs", display_name="test")
        session.add(source)
        session.flush()
        hidden_id = _seed_typed_document(session, source, node_id=ica["id"], marker="a")
        kept_id = _seed_typed_document(session, source, node_id=litigation["id"], marker="b")
        # honestly untyped under an OLD scope: a richer scope must re-judge it
        stale_untyped_id = _seed_typed_document(
            session, source, node_id=None, marker="c", fingerprint="0000000000000000"
        )
        # untyped under the CURRENT scope: already judged, nothing new to try
        _seed_typed_document(session, source, node_id=None, marker="d")
        session.commit()

    def state(object_id: str, stage: str) -> str:
        with factory() as session:
            row = session.scalar(
                select(ProcessingState).where(
                    ProcessingState.source_object_id == object_id,
                    ProcessingState.stage == stage,
                )
            )
            return row.status

    # Phase 1 — same scope as at judging time: only the document untyped under
    # a DIFFERENT (older) scope re-runs; everything judged under the current
    # scope stays put.
    unchanged_config = AppConfig.model_validate({"artifact_dir": str(tmp_path / "artifacts")})
    assert PipelineRunner(factory, unchanged_config).requeue_ontology_outdated() == 1
    assert state(stale_untyped_id, "extract_metadata") == "pending"
    assert state(hidden_id, "extract_metadata") == "done"
    assert state(kept_id, "extract_metadata") == "done"

    # Phase 2 — scope change (Agreements disabled): the doc typed under the
    # now-hidden branch re-runs, and so does the current-scope-untyped doc
    # (any scope change re-judges untyped documents — a richer scope may
    # finally have a home for them). The still-visible doc keeps its result.
    [agreements] = [h for h in base.search("Agreements") if h["label"] == "Agreements"]
    changed_config = AppConfig.model_validate(
        {
            "artifact_dir": str(tmp_path / "artifacts"),
            "ontology": {"disabled_nodes": [agreements["id"]]},
        }
    )
    assert PipelineRunner(factory, changed_config).requeue_ontology_outdated() == 2
    assert state(hidden_id, "extract_metadata") == "pending"
    assert state(hidden_id, "index") == "skipped"
    assert state(hidden_id, "relate") == "done"  # upstream untouched
    assert state(kept_id, "extract_metadata") == "done"


# --- web surface -------------------------------------------------------------


def _client(factory: sessionmaker[Session], tmp_path: Path) -> tuple[TestClient, ConfigStore]:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    return TestClient(create_app(factory, store)), store


def test_ontology_endpoints_browse_search_and_rescope(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, store = _client(factory, tmp_path)
    with client:
        info = client.get("/api/ontology", headers=ADMIN_HEADERS).json()
        assert info["artifact"]["name"] == "lmss"
        assert "lmss" in info["available_artifacts"]
        assert set(info["facets"]) == {"doc_type", "area_of_law", "service", "clause"}
        root_id = info["facets"]["doc_type"]["roots"][0]["id"]

        children = client.get(
            f"/api/ontology/children?node_id={root_id}", headers=ADMIN_HEADERS
        ).json()
        assert children["children"] and all("disabled" in c for c in children["children"])

        hits = client.get("/api/ontology/search?q=intercreditor", headers=ADMIN_HEADERS).json()
        assert hits[0]["label"] == "Intercreditor Agreement"

        branch = children["children"][0]["id"]
        response = client.put(
            "/api/ontology/scope",
            json={"disabled_nodes": [branch]},
            headers=ADMIN_HEADERS,
        ).json()
        assert response["saved"] is True
        assert response["fingerprint"] != info["fingerprint"]
        assert response["requeued_documents"] == 0  # empty corpus

        rechildren = client.get(
            f"/api/ontology/children?node_id={root_id}", headers=ADMIN_HEADERS
        ).json()
        flags = {c["id"]: c for c in rechildren["children"]}
        assert flags[branch]["disabled"] is True and flags[branch]["hidden"] is True
    assert store.get().ontology.disabled_nodes == [branch]


def test_unknown_artifact_is_rejected_before_saving(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    client, store = _client(factory, tmp_path)
    with client:
        response = client.put(
            "/api/ontology/scope", json={"artifact": "no-such"}, headers=ADMIN_HEADERS
        )
        assert response.status_code == 422
    assert store.get().ontology.artifact == "lmss"  # unchanged


def test_health_reports_depth_pressure_and_stale_types(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    base = AppConfig().doc_ontology()
    [ica] = [h for h in base.search("intercreditor") if h["label"] == "Intercreditor Agreement"]
    [transactional] = [
        h for h in base.search("Transactional Document") if h["label"] == "Transactional Document"
    ]
    with factory() as session:
        source = Source(kind="local_fs", display_name="test")
        session.add(source)
        session.flush()
        _seed_typed_document(session, source, node_id=ica["id"], marker="deep")
        _seed_typed_document(session, source, node_id=transactional["id"], marker="shallow")
        session.commit()

    client, _store = _client(factory, tmp_path)
    with client:
        health = client.get("/api/health/doc-types", headers=ADMIN_HEADERS).json()
    branch = health["branches"]["Document Types"]
    assert branch["total"] == 2
    assert branch["shallow"] == 1  # the doc parked at 'Transactional Document'
    [shallow] = health["shallow_nodes"]
    assert shallow["label"] == "Transactional Document" and shallow["count"] == 1
    assert health["untyped_documents"] == 0


def test_discover_artifacts_prefers_uploads_over_packaged(tmp_path: Path) -> None:
    packaged = discover_artifacts(None)
    assert "lmss" in packaged
    uploads = tmp_path / "ontologies"
    uploads.mkdir()
    (uploads / "lmss.json.gz").write_bytes(packaged["lmss"].read_bytes())
    merged = discover_artifacts(uploads)
    assert merged["lmss"] == uploads / "lmss.json.gz"
    # and the upload parses into a working scope
    scope = ontology_scope(merged["lmss"])
    assert len(scope.visible) > 1_000


# --- facet isolation (practice areas) ----------------------------------------


def test_facets_are_isolated_per_consumer() -> None:
    config = AppConfig()
    doc_scope = config.doc_ontology()
    area_scope = config.ontology_facet("area_of_law")
    doc_roots = {r["label"] for r in doc_scope.roots()}
    area_roots = {r["label"] for r in area_scope.roots()}
    # the document-typing agent must never see Area of Law, and vice versa
    assert "Area of Law" not in doc_roots
    assert area_roots == {"Area of Law"}
    assert doc_scope.fingerprint != area_scope.fingerprint
    # the area facet is shallow and human-scale — menu material, not walk material
    assert len(area_scope.visible) < 200


def test_area_menu_is_compact_and_resolvable() -> None:
    area_scope = AppConfig().ontology_facet("area_of_law")
    menu = area_scope.indented_menu()
    lines = menu.splitlines()
    assert len(lines) == len(area_scope.visible)
    # each line is "id  label" and every id resolves in the same scope
    first_id = lines[1].strip().split("  ")[0]
    assert area_scope.resolve(first_id) == first_id
    assert "Tax Law" in menu


def test_service_tools_expose_definitions_under_visited_discipline() -> None:
    scope = AppConfig().ontology_facet("service")
    visited: set[str] = set()
    tools = {tool.name: tool for tool in service_navigation_tools(scope, visited)}
    roots = json.loads(tools["service_roots"].handler({}))
    assert {r["label"] for r in roots} == {"Service"}
    [root] = roots
    kids = json.loads(tools["service_children"].handler({"node_id": root["id"]}))
    assert len(kids["children"]) == 5  # the five kinds of engagement
    # the decisive feature: definitions are readable before submitting
    [txn] = [k for k in kids["children"] if k["label"] == "Transactional Practice"]
    detail = json.loads(tools["service_node"].handler({"node_id": txn["id"]}))
    assert detail["definition"]
    # only ids returned by tools count as visited
    assert txn["id"] in visited
    hits = json.loads(tools["service_search"].handler({"query": "lending"}))
    assert any(h["label"] == "Lending Practice" for h in hits)
    assert {h["id"] for h in hits} <= visited


def test_clause_search_tool_records_visited_and_finds_types() -> None:
    scope = AppConfig().ontology_facet("clause")
    visited: set[str] = set()
    tool = clause_search_tool(scope, visited)
    results = json.loads(tool.handler({"query": "governing law"}))
    assert any(item["label"] == "Governing Law Clause" for item in results)
    assert {item["id"] for item in results} <= visited
    # nonsense finds nothing and pollutes nothing
    before = set(visited)
    assert json.loads(tool.handler({"query": "qwertzuiop"})) == []
    assert visited == before


def test_clause_and_chunk_kind_filters_translate() -> None:
    class _AllowAll:
        def opensearch_filter(self) -> dict:
            return {"match_all": {}}

    query = _combined_filter(
        _AllowAll(), SearchFilters(clause_type="some-clause-node", chunk_kind="clause")
    )
    clauses = query["bool"]["filter"]
    assert {"term": {"clause_type": "some-clause-node"}} in clauses
    assert {"term": {"chunk_kind": "clause"}} in clauses
