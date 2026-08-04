"""Degenerate-agent-loop trips (2026-08-01 run audit): a repeated identical
tool call is warned then aborted, an over-budget conversation is aborted, and
a zombie attempt's failure never overwrites a completed stage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import knowledge_index.pipeline.providers as providers_module
from knowledge_index.config import AppConfig
from knowledge_index.db.models import Blob, ProcessingState, Source, SourceObject
from knowledge_index.pipeline.providers import AgentTool, ModelOutputInvalid, chat_agent
from knowledge_index.pipeline.runner import PipelineRunner
from tests.conftest import TEST_LLM_MODEL


class _Result(BaseModel):
    answer: str


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


def _tool_call_body(name: str, arguments: dict, *, prompt_tokens: int = 1000) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 10},
    }


def _agent() -> tuple[str, AppConfig]:
    """The gateway-served model name and the config, as chat_agent takes them."""
    return TEST_LLM_MODEL, AppConfig()


def _noop_tool(calls: list[dict]) -> AgentTool:
    return AgentTool(
        name="lookup",
        description="a lookup",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: calls.append(args) or "nothing found",
    )


def test_identical_repeated_call_is_warned_then_aborted(monkeypatch) -> None:
    model, config = _agent()
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.setattr(providers_module, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(
        providers_module,
        "_post_to_gateway",
        lambda *a, **k: _FakeResponse(_tool_call_body("lookup", {"q": "same"})),
    )
    executed: list[dict] = []
    with pytest.raises(ModelOutputInvalid, match="degenerate loop"):
        chat_agent(
            model,
            config,
            system="s",
            user="u",
            tools=[_noop_tool(executed)],
            final_schema=_Result,
            max_iters=50,
        )
    # executed only until the warn threshold; warned turns skip the handler
    assert len(executed) == providers_module.REPEATED_CALL_WARN - 1


def test_prompt_token_budget_aborts(monkeypatch) -> None:
    model, config = _agent()
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    monkeypatch.setattr(providers_module, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(
        providers_module,
        "_post_to_gateway",
        lambda *a, **k: _FakeResponse(
            _tool_call_body("lookup", {"q": "x"}, prompt_tokens=200_000)
        ),
    )
    with pytest.raises(ModelOutputInvalid, match="prompt tokens"):
        chat_agent(
            model,
            config,
            system="s",
            user="u",
            tools=[_noop_tool([])],
            final_schema=_Result,
            max_iters=50,
        )


def test_record_failure_never_overwrites_done(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as session:
        session.add(Source(id="src-z", kind="local_fs", display_name="s", config={}))
        session.add(Blob(content_hash="hz", size_bytes=1))
        session.flush()
        session.add(
            SourceObject(
                id="so-z",
                source_id="src-z",
                external_id="ext-z",
                path="/z/file.txt",
                name="file.txt",
                content_hash="hz",
            )
        )
        session.flush()
        session.add(
            ProcessingState(source_object_id="so-z", stage="classify_matter", status="running")
        )
        session.commit()
        state_id = session.scalar(
            select(ProcessingState.id).where(ProcessingState.source_object_id == "so-z")
        )

    runner = PipelineRunner(factory, AppConfig(artifact_dir=tmp_path))

    # zombie attempt holds a stale view (running) ...
    with factory() as zombie_session:
        stale = zombie_session.get(ProcessingState, state_id)
        assert stale.status == "running"
        # ... while the re-dispatched attempt completes the stage
        with factory() as live_session:
            live = live_session.get(ProcessingState, state_id)
            live.status = "done"
            live_session.commit()

        outcome = runner._record_failure(
            zombie_session, stale, RuntimeError("boom"), deterministic=True
        )

    assert outcome == "superseded"
    with factory() as verify:
        assert verify.get(ProcessingState, state_id).status == "done"
