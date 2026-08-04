"""Unit tests for the agentic-classification building blocks.

folder_ls is tested against the real DB; chat_agent's tool loop is tested with a
stubbed gateway so the loop, tool dispatch, and schema-validated submit are locked in
without a live model.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import json

from knowledge_index.config import AppConfig
from knowledge_index.db.models import Artifact, Blob, Matter, ProcessingState, Project, Source, SourceObject
from knowledge_index.pipeline.extraction import MatterClassification
from knowledge_index.pipeline import providers
from knowledge_index.pipeline import runner as runner_module
from knowledge_index.pipeline.folder_context import folder_ls
from knowledge_index.pipeline.matter_search import classification_tools
from knowledge_index.pipeline.providers import AgentTool, chat_agent
from knowledge_index.pipeline.runner import PipelineRunner


def _add_object(session: Session, source: Source, path: str) -> None:
    name = path.rsplit("/", 1)[-1]
    session.add(SourceObject(source_id=source.id, external_id=path, path=path, name=name))


def test_folder_ls_shows_locus_and_two_levels(session: Session) -> None:
    source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
    session.add(source)
    session.flush()
    for path in [
        "Mandate/M-1/Vertraege/nda_final.docx",
        "Mandate/M-1/Vertraege/nda_draft.docx",
        "Mandate/M-1/Schriftsaetze/klage.pdf",
        "Mandate/M-2/other.pdf",
        "Mandate/M-1/Vertraege/Anlagen/anlage1.pdf",
    ]:
        _add_object(session, source, path)
    session.flush()

    listing = folder_ls(session, source.id, "Mandate/M-1/Vertraege/nda_final.docx")
    assert "<-- this document's folder" in listing
    assert "nda_final.docx" in listing and "nda_draft.docx" in listing
    # sibling folder (Schriftsaetze) is visible one level up
    assert "Schriftsaetze" in listing
    # descendant folder within two levels is visible
    assert "Anlagen" in listing and "anlage1.pdf" in listing
    # a different matter's folder (M-2) is out of the ±2 neighbourhood of M-1/Vertraege
    assert "other.pdf" not in listing


class _Decision(BaseModel):
    matter_ref: str
    confidence: float


def test_chat_agent_runs_tools_then_submits(monkeypatch) -> None:
    calls: list[str] = []

    def search(args: dict) -> str:
        calls.append(args.get("query", ""))
        return '[{"matter_ref": "M-2026-0042", "title": "Projekt Falke"}]'

    tools = [
        AgentTool(
            name="search_matters",
            description="search",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=search,
        )
    ]

    # Scripted gateway: first turn calls the search tool, second turn submits the result.
    turns = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_matters",
                                        "arguments": '{"query": "Projekt Falke"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c2",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_result",
                                        "arguments": '{"matter_ref": "M-2026-0042", "confidence": 0.9}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        ]
    )

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return next(turns)

    monkeypatch.setattr(providers.httpx, "post", lambda *a, **k: _Resp())

    result = chat_agent(
        "test",
        AppConfig(),
        system="classify",
        user="{}",
        tools=tools,
        final_schema=_Decision,
        max_iters=4,
    )
    assert result.matter_ref == "M-2026-0042"
    assert result.confidence == 0.9
    assert calls == ["Projekt Falke"]  # the tool actually ran before submit


def test_chat_agent_raises_when_never_submits(monkeypatch, tmp_path: Path) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"choices": [{"message": {"role": "assistant", "content": "thinking..."}}]}

    monkeypatch.setattr(providers.httpx, "post", lambda *a, **k: _Resp())
    try:
        chat_agent(
            "test",
            AppConfig(),
            system="s",
            user="u",
            tools=[],
            final_schema=_Decision,
            max_iters=2,
        )
    except providers.ModelOutputInvalid:
        return
    raise AssertionError("expected ModelOutputInvalid when the agent never submits")


def test_create_matter_tool_is_get_or_create_and_commits(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.commit()
        source_id = source.id

    # Track handler-bound sessions so their read transactions are rolled back
    # before the next test's TRUNCATE reset.
    tool_sessions: list[Session] = []

    def build_tools(seen: set[str]) -> dict[str, AgentTool]:
        session = factory()
        tool_sessions.append(session)
        tools = classification_tools(
            session,
            AppConfig(),
            source_id,
            "Mandate/Falke/vertrag.pdf",
            session_factory=factory,
            project_id=None,
            fallback_reference="UNASSIGNED-FALKE",
            provenance={"model": "test"},
            seen_matter_ids=seen,
        )
        return {tool.name: tool for tool in tools}

    # the enforced create protocol: search first, create as the next action
    seen_a: set[str] = set()
    tools_a = build_tools(seen_a)
    tools_a["search_matters"].handler({"query": "M-2026-0042"})
    created = json.loads(
        tools_a["create_matter"].handler(
            {"reference_number": " m-2026-0042 ", "title": "Projekt Falke"}
        )
    )
    assert created["created"] is True
    assert created["reference_numbers"] == ["M-2026-0042"]
    assert created["id"] in seen_a

    # committed immediately: a session opened after the tool returned sees the matter
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Matter)) == 1

    # a concurrently classifying document's agent gets the existing matter, not a duplicate
    seen_b: set[str] = set()
    tools_b = build_tools(seen_b)
    tools_b["search_matters"].handler({"query": "M-2026-0042"})
    second = json.loads(
        tools_b["create_matter"].handler(
            {"reference_number": "M-2026-0042", "title": "Projekt Falke (Kopie)"}
        )
    )
    assert second["created"] is False
    assert second["id"] == created["id"]
    assert second["id"] in seen_b
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Matter)) == 1

    # without a reference number, the folder-derived fallback keeps the ref deterministic
    tools_c = build_tools(set())
    tools_c["search_matters"].handler({"query": "Neu"})
    fallback = json.loads(tools_c["create_matter"].handler({"title": "Neu"}))
    assert fallback["created"] is True
    assert fallback["reference_numbers"] == ["UNASSIGNED-FALKE"]
    # a placeholder-ref creation is a triage pile, not a real case file
    with factory() as session:
        placeholder = next(
            matter
            for matter in session.scalars(select(Matter))
            if matter.reference_numbers == ["UNASSIGNED-FALKE"]
        )
        assert placeholder.status == "unassigned"

    for tool_session in tool_sessions:
        tool_session.rollback()
        tool_session.close()


def test_classify_matter_reuses_agent_created_matter(
    factory: sessionmaker[Session], monkeypatch
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.flush()
        content_hash = "e" * 64
        session.add(Blob(content_hash=content_hash, size_bytes=4))
        session.flush()
        source_object = SourceObject(
            source_id=source.id,
            external_id="Mandate/Falke/vertrag.pdf",
            path="Mandate/Falke/vertrag.pdf",
            name="vertrag.pdf",
            content_hash=content_hash,
        )
        session.add(source_object)
        session.add(
            Artifact(
                content_hash=content_hash,
                producer="test",
                producer_version="1",
                kind="structured_json",
                payload={"text": "test"},
            )
        )
        session.flush()
        state = ProcessingState(
            source_object_id=source_object.id,
            stage="classify_matter",
            status="running",
        )
        session.add(state)
        session.flush()

        # A faithful stand-in for the real agent: it CALLS the create_matter tool
        # mid-loop (committing the matter), then submits the returned id.
        def fake_chat_agent(*args, **kwargs) -> MatterClassification:
            tools = {tool.name: tool for tool in kwargs["tools"]}
            tools["search_matters"].handler({"query": "M-7"})
            created = json.loads(
                tools["create_matter"].handler(
                    {"reference_number": "M-7", "title": "Projekt Falke"}
                )
            )
            assert created["created"] is True
            return MatterClassification(
                matter_id=created["id"],
                matter_ref="M-7",
                matter_title="Projekt Falke",
                logical_title="Vertrag",
                is_new_matter=True,
                confidence=0.9,
                reasoning="Matter reference in document.",
            )

        monkeypatch.setattr(runner_module, "chat_agent", fake_chat_agent)
        PipelineRunner(factory, AppConfig())._classify_matter(session, state)
        session.flush()

        # the runner adopted the tool-created matter instead of creating a second one
        matters = session.scalars(select(Matter)).all()
        assert len(matters) == 1
        assignment = session.get(
            runner_module.MatterAssignment, source_object.id
        )
        assert assignment is not None
        assert assignment.matter_id == matters[0].id


def test_unprojected_source_does_not_create_project_per_matter(
    factory: sessionmaker[Session], monkeypatch
) -> None:
    with factory() as session:
        source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
        session.add(source)
        session.flush()
        content_hash = "f" * 64
        session.add(Blob(content_hash=content_hash, size_bytes=4))
        session.flush()
        source_object = SourceObject(
            source_id=source.id,
            external_id="Client/M-1/file.txt",
            path="Client/M-1/file.txt",
            name="file.txt",
            content_hash=content_hash,
        )
        session.add(source_object)
        session.add(
            Artifact(
                content_hash=content_hash,
                producer="test",
                producer_version="1",
                kind="structured_json",
                payload={"text": "test"},
            )
        )
        session.flush()
        state = ProcessingState(
            source_object_id=source_object.id,
            stage="classify_matter",
            status="running",
        )
        session.add(state)
        session.flush()

        monkeypatch.setattr(
            runner_module,
            "chat_agent",
            lambda *args, **kwargs: MatterClassification(
                matter_ref="M-1",
                matter_title="Test matter",
                logical_title="Test file",
                doc_type_hint="other",
                is_new_matter=True,
                confidence=0.9,
                reasoning="Matter reference in document.",
            ),
        )
        PipelineRunner(factory, AppConfig())._classify_matter(session, state)
        session.flush()

        assert session.scalar(select(func.count()).select_from(Project)) == 0
        matter = session.scalar(select(Matter))
        assert matter is not None
        assert matter.project_id is None


def test_null_ref_fallback_is_per_folder_honestly_titled_and_flagged(
    factory: sessionmaker[Session], monkeypatch
) -> None:
    """When the agent finds NO reference anywhere, the holding matter is keyed to
    the file's OWN folder (strays of different folders never converge), names
    itself after the folder instead of a member document, and carries status
    "unassigned" so the UI can surface it as a triage pile (audit §4.3)."""

    def classify(path: str) -> None:
        with factory() as session:
            source = session.scalars(select(Source)).first()
            if source is None:
                source = Source(kind="local_fs", display_name="fx", config={"root": "/x"})
                session.add(source)
                session.flush()
            content_hash = f"{abs(hash(path)):064x}"[:64]
            session.add(Blob(content_hash=content_hash, size_bytes=4))
            session.flush()
            source_object = SourceObject(
                source_id=source.id,
                external_id=path,
                path=path,
                name=path.rsplit("/", 1)[-1],
                content_hash=content_hash,
            )
            session.add(source_object)
            session.add(
                Artifact(
                    content_hash=content_hash,
                    producer="test",
                    producer_version="1",
                    kind="structured_json",
                    payload={"text": "no reference anywhere"},
                )
            )
            session.flush()
            state = ProcessingState(
                source_object_id=source_object.id,
                stage="classify_matter",
                status="running",
            )
            session.add(state)
            session.flush()
            monkeypatch.setattr(
                runner_module,
                "chat_agent",
                lambda *a, **k: MatterClassification(
                    matter_id=None,
                    matter_ref=None,
                    matter_title="Should never become the bucket title",
                    logical_title="Scan",
                    confidence=0.2,
                    reasoning="No reference found.",
                ),
            )
            PipelineRunner(factory, AppConfig())._classify_matter(session, state)
            session.commit()

    classify("Clients/Ridgewell/old-scans/scan-1.pdf")
    classify("Clients/Kensington/inbox/fax.pdf")
    classify("Clients/Ridgewell/old-scans/scan-2.pdf")

    with factory() as session:
        matters = session.scalars(select(Matter)).all()
        # different folders -> different holding matters; same folder converges
        assert len(matters) == 2
        by_ref = {matter.reference_numbers[0]: matter for matter in matters}
        ridgewell = by_ref["UNASSIGNED-CLIENTS-RIDGEWELL-OLD-SCANS"]
        kensington = by_ref["UNASSIGNED-CLIENTS-KENSINGTON-INBOX"]
        # honest, folder-derived titles — never a member document's title
        assert ridgewell.title == "Unassigned — Clients/Ridgewell/old-scans"
        assert kensington.title == "Unassigned — Clients/Kensington/inbox"
        # flagged for triage instead of passing as a normal matter
        assert ridgewell.status == "unassigned"
        assert kensington.status == "unassigned"
        assignments = session.scalars(select(runner_module.MatterAssignment)).all()
        assert {
            assignment.matter_id for assignment in assignments
        } == {ridgewell.id, kensington.id}
