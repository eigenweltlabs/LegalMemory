"""A gateway outage is not a verdict on the document.

On the 2026-08-03 51k run LiteLLM ran 8 workers, saturated at 98 req/s, and logged
1,500 timeouts and ~1,200 5xx in five minutes. The pipeline retried each document three
times and then quarantined it — and quarantine is terminal, reachable only by
hand-written SQL. **250 documents were permanently dropped by a capacity problem that
lasted minutes.**

Raising the gateway's worker count fixed that particular saturation, but the
classification defect would drop documents again on the next transport blip. These
tests pin the rule: an infrastructure fault keeps the document retryable forever, while
a genuine poison document still quarantines exactly as before.
"""

from __future__ import annotations

import httpx
import pytest

from knowledge_index.config import AppConfig
from knowledge_index.db.models import ProcessingState
from knowledge_index.pipeline.providers import (
    ProviderPermanentError,
    transient_gateway_fault,
)
from knowledge_index.pipeline.runner import PipelineRunner
from knowledge_index.taxonomies import PipelineStage, ProcessingStatus


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://gateway.invalid/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request)
    )


# --- the classifier -------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadError("read failed"),
        httpx.WriteError("write failed"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.PoolTimeout("pool exhausted"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transport_failures_are_infrastructure(error: Exception) -> None:
    """The httpx storm of the 51k run: EMFILE at 500+ concurrent calls produced
    ReadError and RemoteProtocolError by the thousand."""
    assert transient_gateway_fault(error) is not None


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transient_gateway_statuses_are_infrastructure(status: int) -> None:
    reason = transient_gateway_fault(_status_error(status))
    assert reason is not None
    assert str(status) in reason


@pytest.mark.parametrize("status", [400, 404, 409, 415, 422])
def test_client_errors_are_not_infrastructure(status: int) -> None:
    """A malformed request is the caller's fault and will fail identically forever;
    it must keep its bounded attempt budget."""
    assert transient_gateway_fault(_status_error(status)) is None


@pytest.mark.parametrize(
    "error",
    [ValueError("bad schema"), ProviderPermanentError("dead key"), RuntimeError("boom")],
    ids=lambda e: type(e).__name__,
)
def test_ordinary_errors_are_not_infrastructure(error: Exception) -> None:
    assert transient_gateway_fault(error) is None


def test_a_wrapped_transport_failure_is_still_infrastructure() -> None:
    """A stage handler may re-raise a transport failure inside an error of its own.
    The wrapper must not hide that the cause was the gateway."""
    try:
        try:
            raise httpx.ConnectError("connection refused")
        except httpx.ConnectError as inner:
            raise RuntimeError("classify failed") from inner
    except RuntimeError as outer:
        assert transient_gateway_fault(outer) is not None


def test_the_chain_walk_terminates_on_a_cycle() -> None:
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert transient_gateway_fault(first) is None


# --- the runner's quarantine policy ---------------------------------------------


class _StubSession:
    """_record_failure only reads the row's current status and commits."""

    def __init__(self, status: str = "running") -> None:
        self.status = status
        self.commits = 0

    def scalar(self, _statement):  # noqa: ANN001 - mirrors Session.scalar
        return self.status

    def commit(self) -> None:
        self.commits += 1


def _runner(tmp_path) -> PipelineRunner:
    return PipelineRunner(object(), AppConfig(artifact_dir=tmp_path))


def _state(attempts: int) -> ProcessingState:
    return ProcessingState(
        id="state-1",
        source_object_id="object-1",
        stage=PipelineStage.EXTRACT_METADATA.value,
        status=ProcessingStatus.RUNNING.value,
        attempts=attempts,
    )


def test_an_outage_never_quarantines_however_often_it_recurs(tmp_path) -> None:
    """The defect this file exists for: three strikes against an unreachable gateway
    used to drop the document permanently."""
    runner = _runner(tmp_path)
    max_attempts = runner.config.pipeline.stage(PipelineStage.EXTRACT_METADATA.value).max_attempts

    for attempts in (max_attempts, max_attempts + 1, max_attempts * 20):
        state = _state(attempts)
        outcome = runner._record_failure(
            _StubSession(), state, httpx.ConnectError("connection refused"), deterministic=False
        )
        assert outcome == "retried"
        assert state.status == ProcessingStatus.FAILED.value
        assert state.next_retry_at is not None  # stays claimable by _claim_next
        assert state.last_error["infrastructure"] is not None


def test_a_genuine_failure_still_quarantines_at_the_limit(tmp_path) -> None:
    """The regression guard: relaxing the rule for outages must not relax it for
    everything else."""
    runner = _runner(tmp_path)
    max_attempts = runner.config.pipeline.stage(PipelineStage.EXTRACT_METADATA.value).max_attempts
    state = _state(max_attempts)

    outcome = runner._record_failure(
        _StubSession(), state, RuntimeError("model kept returning nonsense"), deterministic=False
    )

    assert outcome == "quarantined"
    assert state.status == ProcessingStatus.QUARANTINED.value
    assert state.last_error["infrastructure"] is None


def test_a_deterministic_failure_quarantines_on_the_first_attempt(tmp_path) -> None:
    runner = _runner(tmp_path)
    state = _state(1)

    outcome = runner._record_failure(
        _StubSession(), state, ValueError("unsupported document"), deterministic=True
    )

    assert outcome == "quarantined"
    assert state.status == ProcessingStatus.QUARANTINED.value


def test_a_deterministic_verdict_outranks_a_transport_shaped_error(tmp_path) -> None:
    """deterministic=True is the caller's explicit classification and wins: the
    transience check must not resurrect a document the runner already judged."""
    runner = _runner(tmp_path)
    state = _state(1)

    outcome = runner._record_failure(
        _StubSession(), state, httpx.ConnectError("connection refused"), deterministic=True
    )

    assert outcome == "quarantined"
    assert state.last_error["infrastructure"] is None


def test_the_outage_backoff_is_capped(tmp_path) -> None:
    """Unbounded attempts need a bounded delay: 2**attempts reaches days, and a
    document that slept for days would look lost after the gateway recovered."""
    from datetime import UTC, datetime

    from knowledge_index.pipeline.runner import INFRASTRUCTURE_RETRY_MAX_SECONDS

    runner = _runner(tmp_path)
    state = _state(40)  # 2**39 seconds without the cap

    runner._record_failure(
        _StubSession(), state, httpx.ReadError("read failed"), deterministic=False
    )

    delay = (state.next_retry_at - datetime.now(UTC)).total_seconds()
    assert delay <= INFRASTRUCTURE_RETRY_MAX_SECONDS + 5


def test_a_finished_stage_is_never_overwritten(tmp_path) -> None:
    """Pre-existing guard, re-pinned here because _record_failure now has more paths:
    a zombie attempt must not stamp failure over work that completed."""
    runner = _runner(tmp_path)
    state = _state(1)

    outcome = runner._record_failure(
        _StubSession(status=ProcessingStatus.DONE.value),
        state,
        httpx.ConnectError("connection refused"),
        deterministic=False,
    )

    assert outcome == "superseded"
