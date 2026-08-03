"""The usage ledger is written by the gateway client itself.

``/api/costs`` aggregates ``usage_events``. Nothing ever inserted a row, so the cost
centre was structurally zero no matter how much the appliance spent. These tests pin
the contract that closed it: one gateway response with a usage block becomes exactly
one row, attributed to the stage that made the call, and a bookkeeping failure never
propagates into the pipeline.

The gateway is stubbed here — the point under test is the accounting, not the model.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig, ModelSlot
from knowledge_index.db import engine as engine_module
from knowledge_index.db.models import UsageEvent
from knowledge_index.pipeline import providers
from knowledge_index.pipeline.providers import chat_agent, chat_json, usage_stage
from tests.conftest import TEST_LLM_MODEL


class _Answer(BaseModel):
    antwort: str


def _slot(model: str = TEST_LLM_MODEL) -> ModelSlot:
    """A slot with no secret reference: the gateway is stubbed, so resolving a real
    master key here would only couple these tests to the developer's environment."""
    return ModelSlot(model=model, api_key_ref=None)


class _Response:
    """The parts of an httpx response the accounting reads."""

    def __init__(self, payload: dict, headers: dict | None = None) -> None:
        self._payload = payload
        self.headers = headers if headers is not None else {}
        self.elapsed = timedelta(milliseconds=1500)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _completion(content: str, *, usage: dict | None) -> dict:
    payload = {
        "id": "chatcmpl-test",
        "model": TEST_LLM_MODEL,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


@pytest.fixture
def ledger(factory: sessionmaker[Session], monkeypatch) -> sessionmaker[Session]:
    """Point the client's own session factory at the test database.

    The accounting deliberately opens its own session (the cost must survive a stage
    that later rolls back), so the test binds that factory rather than passing one in.
    """
    monkeypatch.setattr(engine_module, "_session_factory", factory)
    return factory


def _rows(factory: sessionmaker[Session]) -> list[UsageEvent]:
    with factory() as session:
        return list(session.scalars(select(UsageEvent).order_by(UsageEvent.created_at)))


def test_a_chat_completion_books_one_row_for_the_stage_that_made_it(ledger, monkeypatch) -> None:
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *a, **k: _Response(
            _completion('{"antwort": "Berlin"}', usage={"prompt_tokens": 88, "completion_tokens": 110}),
            headers={"x-litellm-response-cost": "0.0042", "x-litellm-call-id": "call-1"},
        ),
    )
    config = AppConfig()
    with usage_stage("extract_metadata"):
        chat_json(_slot(), config, system="s", user="u", schema=_Answer)

    rows = _rows(ledger)
    assert len(rows) == 1
    row = rows[0]
    assert row.pipeline_stage == "extract_metadata"
    assert row.provider == "litellm" and row.model == TEST_LLM_MODEL
    assert (row.input_tokens, row.output_tokens) == (88, 110)
    assert row.cost_usd == pytest.approx(0.0042)
    assert row.trace_id == "call-1"
    assert row.details["call"] == "chat"


def test_an_unpriced_model_still_books_its_tokens(ledger, monkeypatch) -> None:
    """A model the gateway cannot price reports 0 USD; the token counts must survive.

    This is the normal case for a self-hosted model, and reporting nothing at all would
    put the whole stage back to looking free."""
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *a, **k: _Response(
            _completion('{"antwort": "x"}', usage={"prompt_tokens": 10, "completion_tokens": 4}),
            headers={"x-litellm-response-cost-original": "0.0"},
        ),
    )
    config = AppConfig()
    with usage_stage("classify_matter"):
        chat_json(_slot("classify-default"), config, system="s", user="u", schema=_Answer)

    (row,) = _rows(ledger)
    assert row.cost_usd == 0.0
    assert (row.input_tokens, row.output_tokens) == (10, 4)


def test_an_agent_loop_books_every_turn_it_actually_spent(ledger, monkeypatch) -> None:
    """Each turn of a tool-using loop is a separate billed call, not one call retried."""
    turns = iter(
        [
            _completion(None, usage={"prompt_tokens": 20, "completion_tokens": 5}),
            _completion('{"antwort": "fertig"}', usage={"prompt_tokens": 30, "completion_tokens": 9}),
        ]
    )
    monkeypatch.setattr(providers.httpx, "post", lambda *a, **k: _Response(next(turns)))
    config = AppConfig()
    with usage_stage("relate"):
        chat_agent(
            _slot(TEST_LLM_MODEL),
            config,
            system="s",
            user="u",
            tools=[],
            final_schema=_Answer,
            max_iters=2,
        )

    rows = _rows(ledger)
    assert [row.input_tokens for row in rows] == [20, 30]
    assert {row.pipeline_stage for row in rows} == {"relate"}
    assert {row.details["call"] for row in rows} == {"agent"}


def test_a_response_without_usage_books_nothing(ledger, monkeypatch) -> None:
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *a, **k: _Response(_completion('{"antwort": "x"}', usage=None)),
    )
    config = AppConfig()
    with usage_stage("extract_decisions"):
        chat_json(_slot(), config, system="s", user="u", schema=_Answer)

    assert _rows(ledger) == []


def test_accounting_failure_never_fails_the_model_call(ledger, monkeypatch, caplog) -> None:
    """The call is already paid for; losing the bookkeeping must not lose the answer.

    It must still be loud — a systematic accounting outage that reported zero spend in
    silence is the failure mode this whole ledger exists to prevent."""
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *a, **k: _Response(
            _completion('{"antwort": "Berlin"}', usage={"prompt_tokens": 1, "completion_tokens": 1})
        ),
    )

    def _broken_session():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(providers, "get_session", _broken_session)
    config = AppConfig()
    with caplog.at_level("WARNING"), usage_stage("index"):
        result = chat_json(_slot(), config, system="s", user="u", schema=_Answer)

    assert result.antwort == "Berlin"
    assert _rows(ledger) == []
    assert any("usage accounting failed" in record.message for record in caplog.records)


def test_a_call_outside_any_stage_is_recorded_as_unassigned(ledger, monkeypatch) -> None:
    """Attribution is never invented: an unattributed call says so rather than guessing."""
    monkeypatch.setattr(
        providers.httpx,
        "post",
        lambda *a, **k: _Response(
            _completion('{"antwort": "x"}', usage={"prompt_tokens": 3, "completion_tokens": 2})
        ),
    )
    config = AppConfig()
    chat_json(_slot(), config, system="s", user="u", schema=_Answer)

    (row,) = _rows(ledger)
    assert row.pipeline_stage is None
