"""Unit tests for the OpenSearch adapter's single-round-trip multi_search.

These stub the HTTP layer so the leg batching, ordering, and skip logic are locked
in without a live cluster (the real queries are covered by the integration suite).
"""

from __future__ import annotations

import pytest

from knowledge_index import search_backend as sb
from knowledge_index.config import AppConfig


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _index(monkeypatch, *, filter_body: dict, responses: list[dict]) -> tuple[sb.OpenSearchIndex, dict]:
    index = sb.OpenSearchIndex(AppConfig())
    monkeypatch.setattr(index, "ensure_index", lambda: None)
    monkeypatch.setattr(sb, "_combined_filter", lambda scope, filters: filter_body)
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["content"] = kwargs.get("content")
        return _FakeResponse({"responses": responses})

    monkeypatch.setattr(sb.httpx, "post", fake_post)
    return index, captured


def test_multi_search_runs_three_legs_in_one_request(monkeypatch) -> None:
    index, captured = _index(
        monkeypatch,
        filter_body={"bool": {"filter": []}},
        responses=[
            {"hits": {"hits": [{"_id": "lex"}]}},
            {"hits": {"hits": [{"_id": "vec"}]}},
            {"hits": {"hits": [{"_id": "ident"}]}},
        ],
    )
    result = index.multi_search(
        query_text="Aktenzeichen 5 O 12/23",
        query_vector=[0.1, 0.2, 0.3, 0.4],
        scope=None,
        filters=None,
        limit=10,
    )
    assert result == {"lexical": [{"_id": "lex"}], "semantic": [{"_id": "vec"}], "identifier": [{"_id": "ident"}]}
    assert captured["url"].endswith("/_msearch")
    # Three active legs -> three header/body pairs -> six NDJSON lines.
    assert captured["content"].decode("utf-8").strip("\n").count("\n") == 5


def test_multi_search_denied_scope_runs_only_semantic(monkeypatch) -> None:
    # A fully-denied scope short-circuits the lexical + identifier legs (they can only
    # return authorized rows), but the semantic leg still runs with the deny filter.
    index, captured = _index(
        monkeypatch,
        filter_body={"match_none": {}},
        responses=[{"hits": {"hits": []}}],
    )
    result = index.multi_search(
        query_text="whatever",
        query_vector=[0.1, 0.2],
        scope=None,
        filters=None,
        limit=5,
    )
    assert result == {"lexical": [], "semantic": [], "identifier": []}
    # Only one leg body was sent (semantic).
    assert captured["content"].decode("utf-8").strip("\n").count("\n") == 1


def test_multi_search_raises_on_leg_error(monkeypatch) -> None:
    index, _ = _index(
        monkeypatch,
        filter_body={"bool": {"filter": []}},
        responses=[
            {"hits": {"hits": []}},
            {"error": {"type": "search_phase_execution_exception"}},
            {"hits": {"hits": []}},
        ],
    )
    with pytest.raises(RuntimeError, match="semantic leg failed"):
        index.multi_search(
            query_text="x", query_vector=[0.1], scope=None, filters=None, limit=5
        )
