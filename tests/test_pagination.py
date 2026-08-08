"""Every list-shaped tool has to be pageable, and honest about what it withheld.

The behaviour under test is not "a limit exists" — it always did. It is that the
limit counts rows the caller actually gets, that there is a way to ask for the
next ones, and that a full page announces itself as a full page. Each test below
corresponds to a way the old surface silently lost results: a limit applied
before the access-control filter, a truncation reported as a total, a rerank that
dropped its own tail, an ordering that made two pages disagree about which row is
which.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import (
    BillingInvoice,
    Blob,
    DecisionRecord,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    Project,
    ProjectGrant,
    Relation,
    Source,
    SourceObject,
)
from knowledge_index.retrieval import RetrievalService, SearchHit
from knowledge_index.retrieval_types import Page
from knowledge_index.web.app import create_app

from tests.test_mcp_citations import _header_auth_config


PRINCIPALS = {"group:paging-test"}
OUTSIDER = {"group:not-invited"}


# --------------------------------------------------------------------------- seed


def _seed_matters(session: Session, count: int, *, visible_from: int = 0) -> None:
    """``count`` matters, one document each, titled so the sort order is known.

    ``visible_from`` puts the first N matters behind a grant the test principal
    does not hold. That is the shape the old code got wrong: it truncated by title
    first and filtered by access second, so an invisible prefix ate the whole page.
    """
    visible_project = Project(
        id="paging-project", key="PP-1", name="Paging project", status="active"
    )
    hidden_project = Project(
        id="hidden-project", key="PP-2", name="Hidden project", status="active"
    )
    source = Source(
        id="paging-source",
        project_id=visible_project.id,
        kind="local_fs",
        display_name="DMS",
        provider="native",
        config={"root": "/srv/dms"},
    )
    session.add_all([visible_project, hidden_project, source])
    session.flush()
    session.add_all(
        [
            ProjectGrant(
                project_id=visible_project.id,
                principal="group:paging-test",
                effect="allow",
                role="viewer",
            ),
            ProjectGrant(
                project_id=hidden_project.id,
                principal="group:someone-else",
                effect="allow",
                role="viewer",
            ),
        ]
    )
    session.flush()

    for index in range(count):
        hidden = index < visible_from
        project_id = hidden_project.id if hidden else visible_project.id
        # Zero-padded so alphabetical order is numeric order.
        tag = f"{index:03d}"
        matter = Matter(
            id=f"matter-{tag}",
            project_id=project_id,
            reference_numbers=[f"M-{tag}"],
            title=f"Matter {tag}",
        )
        blob = Blob(content_hash=f"hash-{tag}", size_bytes=10)
        session.add_all([matter, blob])
        session.flush()
        source_object = SourceObject(
            id=f"source-object-{tag}",
            source_id=source.id,
            external_id=f"external/{tag}",
            path=f"M-{tag}/Agreement.docx",
            name="Agreement.docx",
            container=f"M-{tag}",
            content_hash=blob.content_hash,
        )
        document = Document(
            id=f"document-{tag}",
            project_id=project_id,
            matter_id=matter.id,
            title=f"Agreement {tag}",
            doc_type="contract",
            language="en",
        )
        session.add_all([source_object, document])
        session.flush()
        version = DocumentVersion(
            id=f"version-{tag}",
            document_id=document.id,
            content_hash=blob.content_hash,
            ordinal=1,
            status="final",
        )
        session.add(version)
        session.flush()
        document.latest_final_version_id = version.id
        session.add(
            DocumentVersionSource(
                version_id=version.id, source_object_id=source_object.id
            )
        )
    session.commit()


@pytest.fixture
def service(factory: sessionmaker[Session]):
    def build(session: Session) -> RetrievalService:
        return RetrievalService(session, AppConfig())

    return build


# ------------------------------------------------------------------- Page contract


def test_probe_reports_more_without_counting() -> None:
    page = Page.probe([1, 2, 3], offset=0, limit=2)
    assert page.items == [1, 2]
    assert page.as_dict() == {
        "offset": 0,
        "limit": 2,
        "returned": 2,
        "has_more": True,
        "next_offset": 2,
    }
    # No total: the probe row proves there is more, not how much more.
    assert "total" not in page.as_dict()


def test_a_last_page_says_so() -> None:
    page = Page.probe([1, 2], offset=2, limit=5)
    assert page.as_dict()["has_more"] is False
    assert page.as_dict()["next_offset"] is None


def test_slice_reports_an_exact_total() -> None:
    page = Page.slice(list(range(10)), offset=4, limit=3)
    assert page.items == [4, 5, 6]
    assert page.as_dict() == {
        "offset": 4,
        "limit": 3,
        "returned": 3,
        "has_more": True,
        "next_offset": 7,
        "total": 10,
    }


def test_paging_past_the_end_is_empty_and_final() -> None:
    page = Page.slice(list(range(3)), offset=99, limit=5)
    assert page.items == []
    assert page.as_dict()["has_more"] is False
    assert page.as_dict()["total"] == 3


# ------------------------------------------------------------------- list_matters


def test_list_matters_pages_the_whole_estate(factory, service) -> None:
    with factory() as session:
        _seed_matters(session, 25)

    with factory() as session:
        retrieval = service(session)
        seen: list[str] = []
        offset = 0
        while True:
            page = retrieval.list_matters_page(
                principals=PRINCIPALS, limit=10, offset=offset
            )
            seen.extend(item["id"] for item in page.items)
            if not page.has_more:
                break
            offset = page.offset + len(page.items)
        # Every matter reachable exactly once, in title order.
        assert seen == [f"matter-{index:03d}" for index in range(25)]


def test_the_limit_counts_visible_matters_not_scanned_rows(factory, service) -> None:
    """The regression: 20 matters the caller cannot see used to consume the page.

    Old behaviour applied ``LIMIT`` to all matters ordered by title and only then
    dropped the ones the caller could not read, so a limit of 5 over an invisible
    alphabetical prefix returned nothing at all — and no offset could reach past it.
    """
    with factory() as session:
        _seed_matters(session, 25, visible_from=20)

    with factory() as session:
        page = service(session).list_matters_page(principals=PRINCIPALS, limit=5)
        assert [item["id"] for item in page.items] == [
            f"matter-{index:03d}" for index in range(20, 25)
        ]
        assert page.has_more is False


def test_an_outsider_sees_no_matters(factory, service) -> None:
    with factory() as session:
        _seed_matters(session, 5)

    with factory() as session:
        page = service(session).list_matters_page(principals=OUTSIDER, limit=10)
        assert page.items == []
        assert page.has_more is False


# ---------------------------------------------------------------------- traverse


def _seed_relation_fan(session: Session, edges: int) -> None:
    _seed_matters(session, edges + 1)
    for index in range(1, edges + 1):
        session.add(
            Relation(
                from_type="document",
                from_id="document-000",
                to_type="document",
                to_id=f"document-{index:03d}",
                kind="references",
            )
        )
    session.commit()


def test_traverse_pages_edges(factory, service) -> None:
    with factory() as session:
        _seed_relation_fan(session, 12)

    with factory() as session:
        retrieval = service(session)
        first = retrieval.traverse_page(
            "document", "document-000", principals=PRINCIPALS, limit=5
        )
        assert len(first.items) == 5
        assert first.has_more is True

        second = retrieval.traverse_page(
            "document",
            "document-000",
            principals=PRINCIPALS,
            limit=5,
            offset=first.as_dict()["next_offset"],
        )
        assert len(second.items) == 5
        # Pages do not overlap: the ordering is total, not shard-dependent.
        keys = {(e["from"]["id"], e["to"]["id"]) for e in [*first.items, *second.items]}
        assert len(keys) == 10


def test_traverse_limit_counts_visible_edges(factory, service) -> None:
    """Edges to documents the caller cannot see must not consume page slots."""
    with factory() as session:
        # matters 0..9 hidden, 10..14 visible; every edge starts at a visible doc.
        _seed_matters(session, 15, visible_from=10)
        for index in range(15):
            if index == 10:
                continue
            session.add(
                Relation(
                    from_type="document",
                    from_id="document-010",
                    to_type="document",
                    to_id=f"document-{index:03d}",
                    kind="references",
                )
            )
        session.commit()

    with factory() as session:
        page = service(session).traverse_page(
            "document", "document-010", principals=PRINCIPALS, limit=10
        )
        # Only the 4 edges to other visible documents exist for this caller, and
        # the 10 hidden ones did not eat the page.
        assert len(page.items) == 4
        assert page.has_more is False
        assert all(
            edge["to"]["id"] in {f"document-{i:03d}" for i in range(10, 15)}
            for edge in page.items
        )


# ------------------------------------------------------- find_related_documents


def test_related_documents_report_an_exact_total(factory, service) -> None:
    with factory() as session:
        _seed_relation_fan(session, 8)

    with factory() as session:
        retrieval = service(session)
        first = retrieval.find_related_documents(
            "document-000", principals=PRINCIPALS, limit=3, include_same_matter=False
        )
        assert first["result_count"] == 3
        assert first["page"]["total"] == 8
        assert first["page"]["has_more"] is True
        assert first["page"]["next_offset"] == 3

        last = retrieval.find_related_documents(
            "document-000",
            principals=PRINCIPALS,
            limit=3,
            offset=6,
            include_same_matter=False,
        )
        assert last["result_count"] == 2
        assert last["page"]["has_more"] is False
        assert last["page"]["total"] == 8


def test_related_document_pages_do_not_overlap(factory, service) -> None:
    with factory() as session:
        _seed_relation_fan(session, 8)

    with factory() as session:
        retrieval = service(session)
        collected: list[str] = []
        for offset in (0, 3, 6):
            page = retrieval.find_related_documents(
                "document-000",
                principals=PRINCIPALS,
                limit=3,
                offset=offset,
                include_same_matter=False,
            )
            collected.extend(
                item["document_id"] for item in page["related_documents"]
            )
        assert len(collected) == len(set(collected)) == 8


# --------------------------------------------------------------- search_decisions


def _seed_decisions(session: Session, count: int) -> None:
    _seed_matters(session, 1)
    for index in range(count):
        session.add(
            DecisionRecord(
                id=f"decision-{index:03d}",
                matter_id="matter-000",
                document_id="document-000",
                version_to="version-000",
                locus=f"Clause {index}",
                change_summary="Added a cap",
                rationale_category="risk_allocation",
                rationale_text="The cap limits exposure.",
                source_evidence=[{"source_object_id": "source-object-000"}],
            )
        )
    session.commit()


def test_search_decisions_pages_with_an_exact_total(factory, service) -> None:
    with factory() as session:
        _seed_decisions(session, 7)

    with factory() as session:
        retrieval = service(session)
        first = retrieval.search_decisions_page(
            "cap limits exposure", principals=PRINCIPALS, limit=3
        )
        assert len(first.items) == 3
        assert first.total == 7
        assert first.has_more is True

        rest = retrieval.search_decisions_page(
            "cap limits exposure", principals=PRINCIPALS, limit=10, offset=3
        )
        assert len(rest.items) == 4
        assert rest.has_more is False
        assert {item["id"] for item in first.items} & {
            item["id"] for item in rest.items
        } == set()


# ------------------------------------------------------------------------ rerank


def _hit(version_id: str, score: float) -> SearchHit:
    return SearchHit(
        project_id="p",
        document_id=f"d-{version_id}",
        version_id=version_id,
        matter_id="m",
        title=version_id,
        doc_type=None,
        version_status="final",
        score=score,
        excerpt="",
    )


def test_rerank_keeps_every_hit_it_was_given(monkeypatch) -> None:
    """Rerank reorders a prefix; it must never shrink the result set.

    It used to return only what the model scored, so anything past the 20-hit
    listing vanished — ``limit=50`` with rerank on could not return more than 20
    — and so did any in-window candidate the model forgot to rate. Pagination on
    top of that would have silently skipped rows between pages.
    """
    from knowledge_index import retrieval as retrieval_module

    hits = [_hit(f"v{index:02d}", 1.0 - index / 100) for index in range(30)]

    class _Score:
        def __init__(self, id: str, score: float) -> None:
            self.id = id
            self.score = score

    class _Result:
        # Rates only 3 of the 20 candidates it was shown.
        scores = [_Score("v05", 9.0), _Score("v01", 8.0), _Score("v00", 7.0)]

    monkeypatch.setattr(
        retrieval_module, "chat_json", lambda *args, **kwargs: _Result()
    )
    service = RetrievalService.__new__(RetrievalService)
    service.config = AppConfig()

    ordered = service._rerank("query", hits)

    assert [hit.version_id for hit in ordered[:3]] == ["v05", "v01", "v00"]
    # Nothing lost: the 17 unrated in-window hits and the 10-hit tail are still here.
    assert len(ordered) == 30
    assert {hit.version_id for hit in ordered} == {hit.version_id for hit in hits}


# ------------------------------------------------------------------ ontology paging


def test_ontology_children_page_is_exact() -> None:
    scope = AppConfig().doc_ontology()
    root = scope.roots()[0]["id"]
    everything = scope.children(root)
    if len(everything) < 2:
        pytest.skip("ontology root has too few children to page")

    page = Page.slice(everything, offset=0, limit=1)
    assert page.total == len(everything)
    assert page.has_more is True
    assert page.items == everything[:1]


def test_ontology_search_can_return_every_match() -> None:
    scope = AppConfig().doc_ontology()
    capped = scope.search("agreement", limit=3)
    uncapped = scope.search("agreement", limit=None)
    assert len(capped) <= 3
    assert len(uncapped) >= len(capped)
    # The cap is a window on one ranking, not a different search.
    assert [node["id"] for node in uncapped[: len(capped)]] == [
        node["id"] for node in capped
    ]


# --------------------------------------------------------------- the MCP envelope


def _call(client: TestClient, name: str, arguments: dict | None = None):
    response = client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0",
            "id": name,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "x-ki-principals": "group:paging-test",
        },
    )
    assert response.status_code == 200
    result = response.json()["result"]
    if result.get("isError"):
        return result, None
    structured = result["structuredContent"]
    return result, structured.get("result", structured)


@pytest.fixture
def mcp_client(factory, tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = _header_auth_config(tmp_path / "artifacts")
    store.save(config)
    with TestClient(create_app(factory, store)) as client:
        yield client


def test_list_matters_tool_returns_the_page_envelope(factory, mcp_client) -> None:
    with factory() as session:
        _seed_matters(session, 12)

    _, payload = _call(mcp_client, "list_matters", {"limit": 5})
    assert set(payload) == {"results", "page"}
    assert len(payload["results"]) == 5
    assert payload["page"]["has_more"] is True
    assert payload["page"]["next_offset"] == 5

    _, second = _call(
        mcp_client, "list_matters", {"limit": 5, "offset": payload["page"]["next_offset"]}
    )
    assert [item["id"] for item in second["results"]] == [
        f"matter-{index:03d}" for index in range(5, 10)
    ]


def test_list_invoices_tool_pages_with_a_total(factory, mcp_client) -> None:
    with factory() as session:
        _seed_matters(session, 1)
        for index in range(6):
            session.add(
                BillingInvoice(
                    id=f"invoice-{index}",
                    invoice_number=f"INV-{index}",
                    matter_id="matter-000",
                    invoice_total=100.0,
                    currency="EUR",
                    source_object_id="source-object-000",
                )
            )
        session.commit()

    _, payload = _call(
        mcp_client, "list_invoices", {"matter_id": "matter-000", "limit": 4}
    )
    assert payload["page"]["total"] == 6
    assert payload["page"]["has_more"] is True
    assert len(payload["results"]) == 4

    _, rest = _call(
        mcp_client, "list_invoices", {"matter_id": "matter-000", "limit": 4, "offset": 4}
    )
    assert len(rest["results"]) == 2
    assert rest["page"]["has_more"] is False


def test_billing_tools_do_not_depend_on_a_matter_prefix(factory, mcp_client) -> None:
    """Visibility used to be tested by membership in the first 1,000 matters by
    title, so a matter sorting after that prefix read as unauthorized."""
    with factory() as session:
        _seed_matters(session, 3)
        session.add(
            BillingInvoice(
                id="invoice-late",
                invoice_number="INV-LATE",
                matter_id="matter-002",
                invoice_total=250.0,
                currency="EUR",
                source_object_id="source-object-002",
            )
        )
        session.commit()

    _, rollup = _call(mcp_client, "billing_rollup", {"matter_id": "matter-002"})
    assert rollup.get("authorized") is not False
    _, invoices = _call(mcp_client, "list_invoices", {"matter_id": "matter-002"})
    assert [row["id"] for row in invoices["results"]] == ["invoice-late"]


def test_an_unauthorized_matter_still_answers_with_an_envelope(
    factory, mcp_client
) -> None:
    with factory() as session:
        _seed_matters(session, 2, visible_from=2)
        session.commit()

    _, invoices = _call(mcp_client, "list_invoices", {"matter_id": "matter-000"})
    assert invoices["results"] == []
    assert invoices["page"]["has_more"] is False
    assert invoices["page"]["total"] == 0


def test_a_negative_offset_is_refused(factory, mcp_client) -> None:
    with factory() as session:
        _seed_matters(session, 2)

    result, payload = _call(mcp_client, "list_matters", {"offset": -1})
    assert result["isError"] is True
    assert payload is None
    # The refusal names the parameter, so the caller can fix the call rather than
    # guess that the tool is broken.
    assert "offset" in str(result["content"])


def test_an_oversized_limit_is_clamped_and_reported(factory, mcp_client) -> None:
    """Clamping the limit is safe precisely because the page block says so."""
    with factory() as session:
        _seed_matters(session, 3)

    _, payload = _call(mcp_client, "list_matters", {"limit": 100_000})
    assert payload["page"]["limit"] == 250
    assert payload["page"]["has_more"] is False


def test_resolve_entity_pages_without_repeating_the_first_page(
    factory, mcp_client
) -> None:
    """The window is cut after the citation filter, so an offset must skip
    authorized entities — not the raw resolver rows."""
    from knowledge_index.db.models import Client, MatterClient

    with factory() as session:
        _seed_matters(session, 1)
        for index in range(6):
            client = Client(id=f"client-{index}", name=f"Nordwind {index} GmbH")
            session.add(client)
            session.flush()
            session.add(MatterClient(matter_id="matter-000", client_id=client.id))
        session.commit()

    _, first = _call(mcp_client, "resolve_entity", {"query": "Nordwind", "limit": 2})
    assert len(first["results"]) == 2
    assert first["page"]["has_more"] is True

    _, second = _call(
        mcp_client, "resolve_entity", {"query": "Nordwind", "limit": 2, "offset": 2}
    )
    assert {row["id"] for row in first["results"]} & {
        row["id"] for row in second["results"]
    } == set()


def test_a_ranked_search_refuses_to_page_arbitrarily_deep(mcp_client) -> None:
    """Deep ranked paging is re-ranking, not a cursor. Refusing loudly at the cap
    beats clamping, which is the silent truncation this change removes."""
    result, payload = _call(
        mcp_client, "search_semantic", {"query": "liability", "limit": 100, "offset": 900}
    )
    assert result["isError"] is True
    assert payload is None
    assert "narrow the search" in str(result["content"]).lower()


def test_ontology_children_tool_is_paginated(mcp_client) -> None:
    root = AppConfig().doc_ontology().roots()[0]["id"]
    _, payload = _call(mcp_client, "ontology_children", {"node_id": root, "limit": 1})
    assert set(payload) == {"results", "page"}
    assert payload["page"]["total"] >= len(payload["results"])


def test_ontology_roots_declares_itself_complete(mcp_client) -> None:
    _, payload = _call(mcp_client, "ontology_roots")
    assert payload["complete"] is True
    assert payload["results"]
