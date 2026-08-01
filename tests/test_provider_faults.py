"""A dead billing key is not a rate limit.

OpenAI answers HTTP 429 for two unrelated conditions: `rate_limit_exceeded`, which is a
transient token-per-minute burst, and `insufficient_quota`, which means the account has
no credit and will answer the same way forever. The gateway client used to retry both,
so a quota-dead key held a run in exponential backoff and then quarantined documents
with an HTTP status as their only explanation.

The gateway is stubbed here with the payloads LiteLLM really produces — the shapes below
were taken from this appliance's own gateway (an added model with a deliberately wrong
OpenAI key, and a model id OpenAI does not have). Nothing here claims to have reproduced
a live quota failure; the account behind the live gateway has credit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from knowledge_index.config import AppConfig
from knowledge_index.pipeline import providers
from knowledge_index.pipeline.providers import ProviderPermanentError, embed_text
from tests.conftest import TEST_EMBEDDING_MODEL

# Verbatim from the running gateway: `POST /v1/embeddings` against a model whose
# litellm_params carried an invalid OpenAI key.
INVALID_KEY_BODY = (
    '{"error":{"message":"litellm.AuthenticationError: AuthenticationError: '
    "OpenAIException - Error code: 401 - {'error': {'message': 'Incorrect API key "
    "provided: sk-proj-***. You can find your API key at "
    "https://platform.openai.com/account/api-keys.', 'type': 'invalid_request_error', "
    "'code': 'invalid_api_key', 'param': None}, 'status': 401}. Received Model "
    f'Group={TEST_EMBEDDING_MODEL}\\nAvailable Model Group Fallbacks=None","type":null,'
    '"param":null,"code":"401"}}'
)

# Verbatim from the running gateway: a chat completion against `openai/gpt-nonexistent-9`.
MODEL_NOT_FOUND_BODY = (
    '{"error":{"message":"litellm.NotFoundError: OpenAIException - The model '
    "`gpt-nonexistent-9` does not exist or you do not have access to it.. Received "
    'Model Group=ki-defect-probe-404\\nAvailable Model Group Fallbacks=None",'
    '"type":null,"param":null,"code":"404"}}'
)

# The same wrapping applied to OpenAI's quota response: LiteLLM has no special handling
# for `insufficient_quota`, it maps upstream 429 to RateLimitError and folds the
# provider's payload — code included — into the message string.
INSUFFICIENT_QUOTA_BODY = (
    '{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
    "Error code: 429 - {'error': {'message': 'You exceeded your current quota, please "
    "check your plan and billing details. For more information on this error, read the "
    "docs: https://platform.openai.com/docs/guides/error-codes/api-errors.', 'type': "
    "'insufficient_quota', 'param': None, 'code': 'insufficient_quota'}}. Received "
    f'Model Group={TEST_EMBEDDING_MODEL}\\nAvailable Model Group Fallbacks=None","type":null,'
    '"param":null,"code":"429"}}'
)

# A genuine burst, which must keep its retry.
RATE_LIMIT_BODY = (
    '{"error":{"message":"litellm.RateLimitError: RateLimitError: OpenAIException - '
    "Error code: 429 - {'error': {'message': 'Rate limit reached for "
    f"{TEST_EMBEDDING_MODEL} in organization org-x on tokens per min (TPM): Limit "
    "1000000, Used 999999. Please try again in 1ms.', 'type': 'tokens', 'param': None, "
    "'code': 'rate_limit_exceeded'}}\\\", \\\"code\\\":\\\"429\\\"}}"
)

EMBEDDING = {"data": [{"embedding": [0.0] * 1536}], "model": TEST_EMBEDDING_MODEL}


class _Reply:
    """The parts of an httpx response the gateway client reads."""

    def __init__(self, status_code: int, *, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}
        self.elapsed = None
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def config(tmp_path) -> AppConfig:
    # No secret reference: the gateway is stubbed, so resolving a real master key would
    # only couple this to the developer's environment.
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.retrieval.embedding_model = TEST_EMBEDDING_MODEL
    return config


@pytest.fixture(autouse=True)
def forget_faults():
    """Each test starts with no remembered fault, and leaves none behind."""
    providers.forget_permanent_faults()
    yield
    providers.forget_permanent_faults()


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Backoff is counted, not waited out — the defect was measured in wall-clock time."""
    slept: list[float] = []
    monkeypatch.setattr(providers.time, "sleep", slept.append)
    return slept


def _gateway(monkeypatch, replies: list[_Reply]) -> list[str]:
    """Stub the gateway with a fixed script; return the log of calls actually made."""
    calls: list[str] = []
    queue = list(replies)

    def _post(url: str, **kwargs) -> _Reply:
        calls.append(url)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(providers.httpx, "post", _post)
    return calls


def test_insufficient_quota_fails_on_the_first_call(config, monkeypatch, no_real_sleeping):
    calls = _gateway(monkeypatch, [_Reply(429, text=INSUFFICIENT_QUOTA_BODY)])

    with pytest.raises(ProviderPermanentError) as raised:
        embed_text("Vertragsentwurf", config)

    assert len(calls) == 1  # one attempt, no retry storm
    assert no_real_sleeping == []  # and no backoff burned on it
    message = str(raised.value)
    assert TEST_EMBEDDING_MODEL in message
    assert "no credit left" in message
    assert "Retrying cannot help" in message


def test_a_permanent_failure_is_a_deterministic_stage_failure(config, monkeypatch):
    """The runner quarantines ValueError on the first attempt and retries everything
    else; a permanent provider fault must land on the quarantine side of that line."""
    _gateway(monkeypatch, [_Reply(429, text=INSUFFICIENT_QUOTA_BODY)])

    with pytest.raises(ValueError):
        embed_text("Vertragsentwurf", config)

    assert not issubclass(ProviderPermanentError, providers.ModelOutputInvalid)


def test_a_real_rate_limit_is_still_retried(config, monkeypatch, no_real_sleeping):
    calls = _gateway(
        monkeypatch,
        [
            _Reply(429, text=RATE_LIMIT_BODY),
            _Reply(429, text=RATE_LIMIT_BODY),
            _Reply(200, payload=EMBEDDING),
        ],
    )

    vector = embed_text("Vertragsentwurf", config)

    assert len(vector) == 1536
    assert len(calls) == 3
    assert len(no_real_sleeping) == 2  # it really did back off between attempts


def test_a_rejected_api_key_is_permanent(config, monkeypatch):
    calls = _gateway(monkeypatch, [_Reply(401, text=INVALID_KEY_BODY)])

    with pytest.raises(ProviderPermanentError) as raised:
        embed_text("Vertragsentwurf", config)

    assert len(calls) == 1
    assert "rejected" in str(raised.value)


def test_an_unknown_model_is_permanent(config, monkeypatch):
    calls = _gateway(monkeypatch, [_Reply(404, text=MODEL_NOT_FOUND_BODY)])

    with pytest.raises(ProviderPermanentError):
        embed_text("Vertragsentwurf", config)
    assert len(calls) == 1


def test_a_forbidden_model_is_permanent(config, monkeypatch):
    _gateway(monkeypatch, [_Reply(403, text='{"error":{"message":"model access denied"}}')])

    with pytest.raises(ProviderPermanentError):
        embed_text("Vertragsentwurf", config)


def test_a_server_error_is_not_treated_as_permanent(config, monkeypatch):
    """A 5xx is the gateway having a bad minute — the stage retry owns it."""
    _gateway(monkeypatch, [_Reply(503, text="upstream unavailable")])

    with pytest.raises(httpx.HTTPStatusError):
        embed_text("Vertragsentwurf", config)


def test_a_timeout_is_not_treated_as_permanent(config, monkeypatch):
    def _post(url: str, **kwargs):
        raise httpx.ReadTimeout("gateway did not answer")

    monkeypatch.setattr(providers.httpx, "post", _post)

    with pytest.raises(httpx.ReadTimeout):
        embed_text("Vertragsentwurf", config)


def test_one_dead_account_is_not_rediscovered_by_every_document(config, monkeypatch):
    """500 documents in a run must not mean 500 doomed calls with 500 different stories."""
    calls = _gateway(monkeypatch, [_Reply(429, text=INSUFFICIENT_QUOTA_BODY)])

    reasons = set()
    for _ in range(500):
        with pytest.raises(ProviderPermanentError) as raised:
            embed_text("Vertragsentwurf", config)
        reasons.add(str(raised.value))

    assert len(calls) == 1  # the gateway was asked exactly once
    assert len(reasons) == 1  # and every document quarantines under one readable cause


def test_the_fault_is_forgotten_after_its_cooldown(config, monkeypatch):
    """An administrator who tops the account up is picked up without a worker restart."""
    monkeypatch.setattr(providers, "PERMANENT_FAULT_COOLDOWN_SECONDS", 0.0)
    calls = _gateway(
        monkeypatch,
        [_Reply(429, text=INSUFFICIENT_QUOTA_BODY), _Reply(200, payload=EMBEDDING)],
    )

    with pytest.raises(ProviderPermanentError):
        embed_text("Vertragsentwurf", config)
    assert len(embed_text("Vertragsentwurf", config)) == 1536
    assert len(calls) == 2


def test_the_document_quarantines_on_its_first_attempt_with_a_readable_cause(
    factory, config, monkeypatch, no_real_sleeping
):
    """The whole point, end to end: the real index stage, the real runner, one attempt,
    and a `last_error` a non-engineer can act on."""
    from sqlalchemy import select

    from knowledge_index.db.models import (
        Artifact,
        Blob,
        Document,
        DocumentVersion,
        DocumentVersionSource,
        ProcessingState,
        Source,
        SourceObject,
    )
    from knowledge_index.pipeline import PipelineRunner

    content_hash = "a" * 64
    with factory() as session:
        source = Source(kind="local_fs", display_name="mock firm", config={"root": "/tmp"})
        session.add_all([source, Blob(content_hash=content_hash, size_bytes=32)])
        session.flush()
        source_object = SourceObject(
            source_id=source.id,
            external_id="SPA_final.txt",
            path="M-2026-0042/SPA_final.txt",
            name="SPA_final.txt",
            content_hash=content_hash,
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
        )
        document = Document(title="Unternehmenskaufvertrag")
        session.add_all([source_object, document])
        session.flush()
        version = DocumentVersion(
            document_id=document.id, content_hash=content_hash, ordinal=1, status="final"
        )
        session.add(version)
        session.flush()
        session.add_all(
            [
                DocumentVersionSource(version_id=version.id, source_object_id=source_object.id),
                Artifact(
                    content_hash=content_hash,
                    producer="docling-serve",
                    producer_version="mvp-1",
                    kind="structured_json",
                    payload={"text": "Finaler Unternehmenskaufvertrag."},
                ),
                ProcessingState(source_object_id=source_object.id, stage="index", status="pending"),
            ]
        )
        session.commit()
        object_id = source_object.id

    calls = _gateway(monkeypatch, [_Reply(429, text=INSUFFICIENT_QUOTA_BODY)])
    result = PipelineRunner(factory, config).run_stage_for_object("index", object_id)

    assert result.quarantined == 1
    assert len(calls) == 1
    with factory() as session:
        state = session.scalar(select(ProcessingState).where(ProcessingState.stage == "index"))
        assert state is not None
        assert state.status == "quarantined"
        assert state.attempts == 1  # not three attempts and two backoffs
        assert state.next_retry_at is None
        assert state.last_error["deterministic"] is True
        assert "no credit left" in state.last_error["message"]
        assert "administrator" in state.last_error["message"]


def test_a_fault_on_one_model_does_not_block_another(config, monkeypatch):
    """The account is dead for the model that uses it, not for the whole appliance."""
    quota = _Reply(429, text=INSUFFICIENT_QUOTA_BODY)
    ok = _Reply(200, payload=EMBEDDING)

    def _post(url: str, **kwargs) -> _Reply:
        return quota if kwargs["json"]["model"] == TEST_EMBEDDING_MODEL else ok

    monkeypatch.setattr(providers.httpx, "post", _post)

    with pytest.raises(ProviderPermanentError):
        embed_text("Vertragsentwurf", config)
    config.retrieval.embedding_model = "embedding-local"
    assert len(embed_text("Vertragsentwurf", config)) == 1536
