"""The width of an embedding is requested, not hoped for.

The index is 1536 wide and `embed_text` checked that every vector was 1536 wide,
but it never asked the provider for 1536 — so the width the index got was
whichever width the model returned natively. That held silently for 417k chunks
while OpenAI text-embedding-3-small (native 1536) sat behind the alias, and broke
the moment the same alias was repointed at gemini/gemini-embedding-2, whose native
width is 3072: every document reaching the last stage of the pipeline raised
ModelOutputInvalid and quarantined.

Both models are Matryoshka — they truncate to a requested width — so the fix is
to send the width the check enforces. These tests pin the request, not just the
response, because a check on the response alone is what allowed the drift.
"""

from __future__ import annotations

import httpx
import pytest

from knowledge_index.config import AppConfig
from knowledge_index.pipeline import providers
from knowledge_index.pipeline.providers import ModelOutputInvalid, embed_text
from tests.conftest import TEST_EMBEDDING_MODEL


class _Reply:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self.text = ""
        self.headers: dict[str, str] = {}
        self.elapsed = None
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def config(tmp_path) -> AppConfig:
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.retrieval.embedding_model = TEST_EMBEDDING_MODEL
    return config


@pytest.fixture(autouse=True)
def forget_faults():
    providers.forget_permanent_faults()
    yield
    providers.forget_permanent_faults()


def _capture(monkeypatch, width: int) -> list[dict]:
    """Stub the gateway, record request bodies, answer with `width` floats."""
    bodies: list[dict] = []

    def _post(url: str, **kwargs) -> _Reply:
        bodies.append(kwargs.get("json") or {})
        return _Reply({"data": [{"embedding": [0.0] * width}], "model": TEST_EMBEDDING_MODEL})

    monkeypatch.setattr(providers.httpx, "post", _post)
    return bodies


def test_the_request_states_the_width_the_index_expects(config, monkeypatch):
    bodies = _capture(monkeypatch, config.retrieval.embedding_dimensions)

    embed_text("Vertragsentwurf", config)

    assert bodies[0]["dimensions"] == config.retrieval.embedding_dimensions


def test_the_stated_width_follows_the_config_rather_than_a_constant(config, monkeypatch):
    """A deployment that rebuilds its index at another width must not have 1536
    hardcoded into the call underneath it."""
    config.retrieval.embedding_dimensions = 768
    bodies = _capture(monkeypatch, 768)

    embed_text("Vertragsentwurf", config)

    assert bodies[0]["dimensions"] == 768


def test_the_model_and_input_are_still_sent(config, monkeypatch):
    bodies = _capture(monkeypatch, config.retrieval.embedding_dimensions)

    embed_text("Vertragsentwurf", config)

    assert bodies[0]["model"] == TEST_EMBEDDING_MODEL
    assert bodies[0]["input"] == ["Vertragsentwurf"]


def test_a_native_width_response_is_the_failure_that_was_observed(config, monkeypatch):
    """gemini-embedding-2's native 3072 against a 1536 index. Kept failing loudly:
    a provider that ignores the requested width must never write into the index,
    because a silently half-truncated vector is unrecoverable once stored."""
    _capture(monkeypatch, 3072)

    with pytest.raises(ModelOutputInvalid) as raised:
        embed_text("Vertragsentwurf", config)

    assert "3072" in str(raised.value)
    assert "1536" in str(raised.value)


def test_a_dropped_dimensions_field_surfaces_as_a_width_mismatch(config, monkeypatch):
    """LiteLLM's drop_params removes `dimensions` for a provider with no equivalent.
    That must land on the same loud failure rather than a quiet wrong-width write."""
    _capture(monkeypatch, 3072)

    with pytest.raises(ModelOutputInvalid):
        embed_text("Vertragsentwurf", config)


def test_the_vector_is_returned_as_floats_at_the_expected_width(config, monkeypatch):
    _capture(monkeypatch, config.retrieval.embedding_dimensions)

    vector = embed_text("Vertragsentwurf", config)

    assert len(vector) == config.retrieval.embedding_dimensions
    assert all(isinstance(value, float) for value in vector)


def test_the_index_name_is_unchanged_by_this_fix(config):
    """The 417k chunks already written are addressed by model slug + width. This
    change restores the width; it must not move the index they live in."""
    assert str(config.retrieval.embedding_dimensions) in config.embedding_signature()
    assert config.derived_index_name().endswith(config.embedding_signature())


def test_an_http_failure_is_not_swallowed_by_the_new_field(config, monkeypatch):
    def _post(url: str, **kwargs) -> _Reply:
        raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(providers.httpx, "post", _post)
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)

    with pytest.raises(Exception) as raised:
        embed_text("Vertragsentwurf", config)

    assert not isinstance(raised.value, ModelOutputInvalid)
