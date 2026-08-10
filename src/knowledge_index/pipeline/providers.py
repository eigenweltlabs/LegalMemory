"""Model gateway client. Every model call goes through LiteLLM (or any
OpenAI-compatible endpoint). There are no offline substitutes: gateway errors
propagate so the pipeline's retry/quarantine semantics own the failure.

Because every model call funnels through here, this is also where spend is booked:
one UsageEvent row per gateway response, attributed to the stage that made the call.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from knowledge_index.config import AppConfig
from knowledge_index.db import get_session
from knowledge_index.db.models import UsageEvent

SchemaT = TypeVar("SchemaT", bound=BaseModel)

log = logging.getLogger(__name__)

# Reasoning-capable extraction models can legitimately need several minutes for
# large documents. Keep the client timeout above LiteLLM's gateway timeout so a
# gateway 408 is reported cleanly and owned by the pipeline retry policy.
# A matter-level agent reading a large folder legitimately runs for many turns, and
# the previous 660s cut those off mid-conversation. The failure surfaced as a gateway
# 408, burned a stage attempt, and quarantined a document that was doing nothing
# wrong -- 43 of 52 quarantines on the 9,288-file run, a different set every time,
# because it depends on what is in flight rather than on the file. Must stay above
# the gateway's own request timeout or the gateway cuts first and this never applies.
MODEL_REQUEST_TIMEOUT_SECONDS = float(os.getenv("KI_MODEL_REQUEST_TIMEOUT_SECONDS", "1980"))

# Degenerate-agent-loop trip (see chat_agent): warn on the Nth identical
# back-to-back tool call, abort on the Mth. This catches an agent that has stopped
# making progress, which is a real fault.
#
# There is deliberately NO prompt-size budget. One used to abort the conversation at
# 160k tokens "well below the serving context", and it did not prevent runaway loops
# -- the repeated-call trip above does that -- it only killed the agents doing the
# most work. On a 9,288-file corpus it quarantined documents whose folders were
# large or whose text was sprawling (tracked-changes markup, spreadsheet trackers,
# long email threads), and it did so deterministically: those files failed every
# attempt, on every retry, at any concurrency, so they were the only documents the
# pipeline could never index. A conversation that genuinely exceeds the model's
# serving context gets a real error from the gateway saying so.
REPEATED_CALL_WARN = int(os.getenv("KI_AGENT_REPEATED_CALL_WARN", "3"))
REPEATED_CALL_ABORT = int(os.getenv("KI_AGENT_REPEATED_CALL_ABORT", "6"))

# One dead billing key must not be rediscovered once per document. After a permanent
# fault the same model fails fast for this long without touching the network, so a
# 500-file estate quarantines in seconds under one identical, readable cause instead of
# making 500 doomed calls. Kept short so an administrator who tops up the account or
# fixes the key is picked up on the next attempt, with no worker restart.
PERMANENT_FAULT_COOLDOWN_SECONDS = float(
    os.getenv("KI_PROVIDER_FAULT_COOLDOWN_SECONDS", "60")
)

# The stage that owns the model calls made on this thread/task. Set by whoever owns a
# unit of work (a pipeline stage claim, a search, an assistant answer) rather than
# threaded through every signature: one model may serve several stages, and the helpers
# in between — tool loops, retrieval legs, billing extraction — must not have to forward it.
_USAGE_STAGE: ContextVar[str | None] = ContextVar("knowledge_index_usage_stage", default=None)


@contextmanager
def usage_stage(stage: str) -> Iterator[None]:
    """Attribute every model call made inside this block to ``stage``."""
    token = _USAGE_STAGE.set(stage)
    try:
        yield
    finally:
        _USAGE_STAGE.reset(token)


@dataclass
class AgentTool:
    """One tool an agent may call. ``handler`` receives the parsed arguments and
    returns a string result that is fed back to the model."""

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]


class ModelOutputInvalid(ValueError):
    """The model returned output that does not validate against the stage schema.

    Subclasses ValueError for callers that handle invalid typed output explicitly.
    The pipeline retries it up to the stage's bounded max-attempts because truncated,
    empty, or incomplete reasoning-model responses are commonly transient."""


class ProviderPermanentError(ValueError):
    """The gateway refused the call for a reason no repetition can change.

    A dead billing key, a revoked API key, a model the gateway does not serve: the
    next identical request fails identically. Deliberately a ValueError, because the
    pipeline runner already classifies ValueError as a deterministic stage failure —
    so the document quarantines on its FIRST attempt carrying this message as its
    reason, instead of spending the stage's attempt budget and its exponential backoff
    on a fault that will still be there afterwards.

    The message is written for the person who has to fix it, not for a log parser: it
    names the model, says what the provider objected to, and says what to do about it.
    """


# Statuses that are permanent whatever the body says. Evidence: LiteLLM gives these
# no distinct wire shape — it maps the upstream status through and folds the provider's
# own payload into the message string. Observed against this appliance's own gateway
# (deliberately wrong OpenAI key, then a model id OpenAI does not have):
#   401 {"error":{"message":"litellm.AuthenticationError: AuthenticationError:
#        OpenAIException - Error code: 401 - {'error': {…'code': 'invalid_api_key'…}}"}}
#   404 {"error":{"message":"litellm.NotFoundError: OpenAIException - The model
#        `gpt-nonexistent-9` does not exist or you do not have access to it."}}
# so the status is the reliable signal and the body is what makes the message readable.
# 5xx, 408 and connection errors are deliberately absent: those are the transient
# faults the stage retry exists for.
_PERMANENT_STATUS_CAUSES = {
    401: "the API key the gateway presented was rejected (unauthenticated)",
    403: "the API key is not permitted to use this model (forbidden)",
    404: "the gateway does not serve a model under this name",
}

# HTTP 429 means two unrelated things at OpenAI, and only the body tells them apart:
# `rate_limit_exceeded` is a transient token-per-minute burst that must be retried,
# `insufficient_quota` is an account with no credit left, which retrying can never fix.
# Conflating them is the defect: a quota-dead key kept a run in the backoff loop for 34
# minutes and then quarantined documents with no cause an operator could read.
_DEAD_ACCOUNT_MARKERS = (
    "insufficient_quota",  # OpenAI / Azure OpenAI: prepaid credit exhausted
    "billing_hard_limit_reached",  # OpenAI: the account's own hard spend cap
    "exceeded your current quota",  # the same condition in prose, if only it survives
)


def embed_text(text: str, config: AppConfig) -> list[float]:
    model = config.retrieval.embedding_model
    base = gateway_url(config)
    response = _post_to_gateway(
        f"{base}/v1/embeddings",
        model=model,
        base=base,
        headers=_headers(),
        json={"model": model, "input": [text]},
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    _record_usage(response, body, model, call="embedding")
    vector = list(body["data"][0]["embedding"])
    expected = config.retrieval.embedding_dimensions
    if len(vector) != expected:
        raise ModelOutputInvalid(
            f"embedding model returned {len(vector)} dimensions; index expects {expected}"
        )
    return [float(value) for value in vector]


def _trace_metadata(trace_tags: list[str] | None) -> dict:
    """LiteLLM request tags: every gateway log row carries the document,
    stage, and a never-recurring per-attempt trace id, so full-message traces
    are filterable per document/stage and addressable per attempt."""
    if trace_tags:
        return {"tags": trace_tags}
    return {}


def chat_json(
    model: str,
    config: AppConfig,
    *,
    system: str,
    user: str,
    schema: type[SchemaT],
    max_output_tokens: int | None = None,
    trace_tags: list[str] | None = None,
) -> SchemaT:
    """One structured chat completion, validated against the stage's schema.

    ``model`` is the name the LiteLLM gateway serves the model under. The JSON schema
    is embedded in the system prompt and enforced client-side by pydantic; an invalid
    payload raises ModelOutputInvalid for bounded stage retry."""

    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    base = gateway_url(config)
    response = _post_to_gateway(
        f"{base}/v1/chat/completions",
        model=model,
        base=base,
        headers=_headers(),
        json={
            "model": model,
            "temperature": 0.0,  # pipeline output is deterministic by design
            # No output cap by default. This model reasons before it answers, and
            # a cap truncates the reasoning rather than shortening the answer:
            # measured on one classification, 12,000 tokens went entirely into a
            # 29,904-character reasoning trace and `content` came back None. That
            # surfaces as ModelOutputInvalid, the document's extraction is lost,
            # and — because the matter's practice area is set by the first
            # document that survives — an unlucky survivor defines the matter (a
            # $700M term loan filed under Real Property Law because a mortgage
            # form won the lottery). 1,441 documents were quarantined this way in
            # one 51k run. A cap here buys nothing the stage timeout does not.
            **({"max_tokens": max_output_tokens} if max_output_tokens else {}),
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system}\n\nRespond with a single JSON object only, "
                        f"matching this JSON schema exactly:\n{schema_json}"
                    ),
                },
                {"role": "user", "content": user},
            ],
            **_trace_metadata(trace_tags),
        },
        timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    _record_usage(response, body, model, call="chat")
    content = body["choices"][0]["message"]["content"]
    try:
        return schema.model_validate_json(content)
    except ValidationError as exc:
        raise ModelOutputInvalid(
            f"model {model} returned schema-invalid output: {exc}"
        ) from exc


def chat_agent(
    model: str,
    config: AppConfig,
    *,
    system: str,
    user: str,
    tools: list[AgentTool],
    final_schema: type[SchemaT],
    max_iters: int | None = None,
    max_output_tokens: int | None = None,
    max_tool_result_chars: int | None = None,
    result_validator: Callable[[SchemaT], str | None] | None = None,
    trace_tags: list[str] | None = None,
) -> SchemaT:
    """A bounded tool-using agent loop, validated against ``final_schema``.

    The model may call the supplied ``tools`` to gather evidence (e.g. search existing
    matters, list the folder neighbourhood) before answering. It finishes by calling
    the synthetic ``submit_result`` tool whose arguments ARE the final schema, so the
    return value is always schema-validated — like ``chat_json``, but with a search
    loop. Bounded by ``max_iters``; exhausting it raises ``ModelOutputInvalid`` so the
    stage can retry the whole interaction up to its configured max attempts."""

    submit = "submit_result"
    tool_specs: list[dict] = [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]
    tool_specs.append(
        {
            "type": "function",
            "function": {
                "name": submit,
                "description": "Submit your final answer. Call exactly once when confident.",
                "parameters": final_schema.model_json_schema(),
            },
        }
    )
    registry = {t.name: t.handler for t in tools}
    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                f"{system}\n\nCall tools to gather evidence before deciding. When ready, "
                f"finish by calling `{submit}` exactly once with your final answer. Do not "
                f"answer in free text — always finish with `{submit}`."
            ),
        },
        {"role": "user", "content": user},
    ]
    base = gateway_url(config)
    headers = _headers()

    # Degenerate-loop defenses (2026-08-01 run: one classify attempt repeated the
    # identical tool call ~70 turns, 9.8M prompt tokens, and died on the context
    # wall an hour later). Two independent trips, both ending in a clean stage
    # retry instead of an hour-long hang:
    #  - the same (tool, arguments) call repeated back-to-back is warned once,
    #    then aborted;
    #  - the conversation's prompt size is budgeted well below the serving
    #    context, using the gateway's own reported prompt_tokens.
    repeat_signature: str | None = None
    repeat_count = 0

    # No turn limit by default. Every cap on this loop has cost us more than it
    # saved: the agent stops mid-investigation and raises ModelOutputInvalid, the
    # stage retries the whole interaction, and if it keeps failing the document's
    # metadata is decided by whatever survived — which is how a $700M term loan
    # came to be filed under Real Property Law. An agent that has not answered yet
    # needs another turn, not a refusal.
    iteration = -1
    while max_iters is None or iteration + 1 < max_iters:
        iteration += 1
        # Give the agent freedom to inspect evidence, but make a BOUNDED loop
        # actually converge: on its final permitted turn it must submit the typed
        # result rather than spend its last chance on another read-only call.
        tool_choice: str | dict = "auto"
        if max_iters is not None and iteration == max_iters - 1:
            tool_choice = {"type": "function", "function": {"name": submit}}
        response = _post_to_gateway(
            f"{base}/v1/chat/completions",
            model=model,
            base=base,
            headers=headers,
            json={
                "model": model,
                "temperature": 0.0,  # pipeline output is deterministic by design
                    **({"max_tokens": max_output_tokens} if max_output_tokens else {}),
                "tools": tool_specs,
                "tool_choice": tool_choice,
                "messages": messages,
                **_trace_metadata(trace_tags),
            },
            timeout=MODEL_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        # One row per gateway turn: an agent loop is several real, separately billed
        # calls, not one call retried.
        _record_usage(response, body, model, call="agent")
        message = body["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            content = message.get("content") or ""
            try:
                candidate = final_schema.model_validate_json(content)
            except ValidationError:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {"role": "user", "content": f"Finish by calling `{submit}` with a valid result."}
                )
                continue
            validation_error = result_validator(candidate) if result_validator else None
            if validation_error:
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {"role": "user", "content": f"Invalid result: {validation_error}. Fix it."}
                )
                continue
            return candidate

        messages.append(message)
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == submit:
                try:
                    candidate = final_schema.model_validate(args)
                except ValidationError as exc:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": f"Invalid result: {exc}. Fix and resubmit.",
                        }
                    )
                    continue
                validation_error = result_validator(candidate) if result_validator else None
                if validation_error:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": f"Invalid result: {validation_error}. Fix and resubmit.",
                        }
                    )
                    continue
                return candidate
            signature = f"{name}:{fn.get('arguments') or ''}"
            if signature == repeat_signature:
                repeat_count += 1
            else:
                repeat_signature, repeat_count = signature, 1
            if repeat_count >= REPEATED_CALL_ABORT:
                raise ModelOutputInvalid(
                    f"agent {model} repeated the identical tool call {name!r} "
                    f"{repeat_count} times in a row — degenerate loop aborted"
                )
            if repeat_count >= REPEATED_CALL_WARN:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": (
                            f"you have made this exact {name} call {repeat_count} times — "
                            "its result does not change. Use what you already have, make a "
                            f"DIFFERENT call, or finish with `{submit}`."
                        ),
                    }
                )
                continue
            handler = registry.get(name)
            if handler is None:
                result = f"unknown tool: {name}"
            else:
                try:
                    result = handler(args if isinstance(args, dict) else {})
                except Exception as exc:  # a tool failure is fed back, not fatal
                    result = f"tool error: {type(exc).__name__}: {exc}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    # Never truncated by default: a clipped tool result is
                    # evidence cut mid-structure, and the agent then reasons from
                    # a payload that looks complete and is not.
                    "content": (
                        str(result)[:max_tool_result_chars]
                        if max_tool_result_chars
                        else str(result)
                    ),
                }
            )

    raise ModelOutputInvalid(
        f"agent {model} did not submit a valid result within {max_iters} iterations"
    )  # only reachable when a caller opted into a turn limit


def _record_usage(response: httpx.Response, body: dict, model: str, *, call: str) -> None:
    """Book one UsageEvent for one gateway response.

    Called once per HTTP response that the gateway actually answered, so a stage that
    retries is billed for every attempt it really made and for none it did not — the
    gateway itself never retries (router_settings.num_retries is 0).

    The row is written on its own short-lived session rather than the caller's: the
    call has already been paid for, so the cost must survive a stage that later fails
    and rolls back. Accounting can never fail a model call, but a systematic failure
    (schema drift, unreachable database) is logged with its traceback rather than
    swallowed — silent zero spend is exactly the bug this exists to prevent.
    """
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return  # nothing to account for: no usage block, no billed call
    try:
        headers = getattr(response, "headers", None) or {}
        # LiteLLM names the header differently per route; the plain one wins when both
        # are present. A model with no entry in the gateway's cost map reports 0.0 —
        # a real zero, not a missing measurement, and the token counts still land.
        cost = headers.get("x-litellm-response-cost") or headers.get(
            "x-litellm-response-cost-original"
        )
        elapsed = getattr(response, "elapsed", None)
        details = {"call": call, "gateway_model": body.get("model"), "response_id": body.get("id")}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if reasoning:
            details["reasoning_tokens"] = int(reasoning)
        with get_session() as session:
            session.add(
                UsageEvent(
                    pipeline_stage=_USAGE_STAGE.get(),
                    provider="litellm",
                    model=model,
                    input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                    output_tokens=int(
                        usage.get("completion_tokens") or usage.get("output_tokens") or 0
                    ),
                    cost_usd=float(cost) if cost is not None else 0.0,
                    duration_ms=elapsed.total_seconds() * 1000 if elapsed is not None else None,
                    trace_id=headers.get("x-litellm-call-id"),
                    details=details,
                )
            )
    except Exception:
        log.warning("usage accounting failed for model %s (%s)", model, call, exc_info=True)


def gateway_url(config: AppConfig) -> str:
    """Base URL of the shared LiteLLM gateway — every model call goes here."""
    return config.components.litellm_url.rstrip("/")


def gateway_admin_headers() -> dict[str, str]:
    """Master-key headers for LiteLLM's admin API (model registry, credentials).

    The master key must never leave this process — the browser talks to the app,
    only the app talks to the gateway's admin routes."""
    key = os.environ.get("LITELLM_MASTER_KEY")
    if not key:
        raise ValueError(
            "LITELLM_MASTER_KEY is not set; the gateway's model registry is unreachable"
        )
    return {"authorization": f"Bearer {key}", "content-type": "application/json"}


# Remembered permanent faults, keyed by (gateway base URL, model name): the fault
# belongs to the account behind one model, not to the document that happened to find it.
# Per process, so a worker that is restarted re-probes immediately.
_permanent_faults: dict[tuple[str, str], tuple[float, str]] = {}
_permanent_faults_lock = threading.Lock()


def forget_permanent_faults() -> None:
    """Drop the remembered permanent faults (config change, tests)."""
    with _permanent_faults_lock:
        _permanent_faults.clear()


def _remembered_fault(key: tuple[str, str]) -> str | None:
    with _permanent_faults_lock:
        entry = _permanent_faults.get(key)
        if entry is None:
            return None
        expires_at, message = entry
        if time.monotonic() >= expires_at:
            del _permanent_faults[key]
            return None
        return message


def _remember_fault(key: tuple[str, str], message: str) -> None:
    with _permanent_faults_lock:
        _permanent_faults[key] = (
            time.monotonic() + PERMANENT_FAULT_COOLDOWN_SECONDS,
            message,
        )


def _permanent_cause(response) -> str | None:
    """Why this response can never succeed on repetition — or None if it might."""
    status = getattr(response, "status_code", None)
    if status == 429:
        body = str(getattr(response, "text", "") or "").lower()
        if any(marker in body for marker in _DEAD_ACCOUNT_MARKERS):
            return (
                "the account behind this model has no credit left — the provider "
                "answered `insufficient_quota`, not a rate limit"
            )
        return None  # a real token-per-minute burst: back off and try again
    return _PERMANENT_STATUS_CAUSES.get(status)


def _permanent_error_message(model: str, response, cause: str) -> str:
    detail = " ".join(str(getattr(response, "text", "") or "").split())[:400]
    return (
        f"Model '{model}' cannot be used: {cause}. Retrying cannot help — an "
        f"administrator has to fix the model account or the gateway configuration, "
        f"then requeue the affected documents. "
        f"Gateway answered HTTP {getattr(response, 'status_code', '?')}: {detail}"
    )


def _post_to_gateway(
    url: str, *, model: str, base: str, **kwargs
) -> httpx.Response:
    """Retry a transient gateway 429 in-place; fail fast on a permanent refusal.

    LiteLLM forwards OpenAI's token-per-minute response. Retrying that here preserves
    the same durable stage attempt and avoids Hatchet emitting a full failed-task
    traceback for every transient rate-limit response.

    A permanent refusal (dead account, rejected key, unknown model) is the opposite
    case: every retry burns wall-clock time on an answer that will not change, and the
    real cause never reaches the operator. Those raise ProviderPermanentError on the
    first response, and the fault is remembered briefly so the rest of the corpus fails
    fast under the same message rather than each document rediscovering it.
    """
    fault_key = (base, model)
    remembered = _remembered_fault(fault_key)
    if remembered is not None:
        log.debug("skipping %s call: %s", model, remembered)
        raise ProviderPermanentError(remembered)
    for attempt in range(7):
        response = httpx.post(url, **kwargs)
        cause = _permanent_cause(response)
        if cause is not None:
            message = _permanent_error_message(model, response, cause)
            _remember_fault(fault_key, message)
            # The cause in plain words, without the gateway's raw body: that goes into
            # the exception (and so into the quarantine reason an admin reads in the UI),
            # where an error payload that echoes part of a credential is at least behind
            # an admin login rather than in the container log.
            log.error(
                "model %s is unusable: %s (gateway answered HTTP %s)",
                model,
                cause,
                getattr(response, "status_code", "?"),
            )
            raise ProviderPermanentError(message)
        if getattr(response, "status_code", None) != 429 or attempt == 6:
            return response
        base_delay = min(30.0, float(2**attempt))
        retry_after = response.headers.get("retry-after")
        try:
            requested_delay = float(retry_after) if retry_after else 0.0
        except ValueError:
            requested_delay = 0.0
        delay = max(base_delay, requested_delay)
        time.sleep(delay + random.uniform(0.0, delay * 0.25))
    raise RuntimeError("unreachable")


def _headers() -> dict[str, str]:
    headers = {"content-type": "application/json"}
    key = os.environ.get("LITELLM_MASTER_KEY")
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers
