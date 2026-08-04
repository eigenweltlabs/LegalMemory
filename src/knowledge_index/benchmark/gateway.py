"""Direct LiteLLM-gateway chat helpers for the benchmark agent + judge.

Kept separate from ``pipeline.providers.chat_json`` (which injects a German schema
instruction) so vendored prompts — like the vendored rubric judge — are sent verbatim.
``httpx`` is imported lazily so this module stays offline-importable.
"""

from __future__ import annotations

import os

import json
import re

from knowledge_index.config import AppConfig

#: How long one model call may take before the harness gives up on it. Deliberately
#: generous: this bounds the BENCHMARK's patience, not the system's behaviour, and a
#: timeout here is indistinguishable in the results from the system getting an answer
#: wrong. Latency is reported per run, so a slow system is still visibly slow.
_REQUEST_TIMEOUT_SECONDS = 900


def complete(
    config: AppConfig,
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    max_tokens: int = 4000,
    usage_sink: dict | None = None,
) -> dict:
    """One chat completion through the gateway; returns the raw message dict.

    Retries on 429 (rate-limit blips) AND transient faults — 5xx and connection
    errors — with capped exponential backoff. A fleet backend scaling away mid-run
    surfaces as a brief LB 503; without the retry, one such blip killed an entire
    200-query eval config. When ``usage_sink`` is given, the response's token usage
    is accumulated into it (for per-run cost).
    """
    import time

    import httpx


    base = config.components.litellm_url.rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    payload: dict = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    # An agent that reads several documents accumulates a very large prompt — measured
    # up to 145k tokens on the full tool surface, which the model needs 600-720s to
    # prefill. At the old 240s the client abandoned calls that would have SUCCEEDED,
    # then retried into the same wall eight times, so those items could never complete
    # and scored as failures. Worse, they were not a random subset: a context gets that
    # large precisely because the request was hard. This is a harness bound, not a
    # model one, so it is set above the observed worst case rather than tuned to it.
    # ``timeout`` is also sent in the payload because the gateway applies its own
    # request_timeout (600s) and would cut the call off first.
    payload["timeout"] = _REQUEST_TIMEOUT_SECONDS
    headers = {"authorization": f"Bearer {key}"} if key else {}
    for attempt in range(8):
        try:
            response = httpx.post(
                f"{base}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            if attempt < 7:
                time.sleep(min(2**attempt, 30))
                continue
            raise
        if (response.status_code == 429 or response.status_code >= 500) and attempt < 7:
            # honor the server's Retry-After when present; otherwise capped exp backoff.
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after else min(2**attempt, 30)
            time.sleep(delay)
            continue
        response.raise_for_status()
        body = response.json()
        if usage_sink is not None:
            u = body.get("usage") or {}
            for key_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage_sink[key_name] = usage_sink.get(key_name, 0) + int(u.get(key_name) or 0)
            usage_sink["calls"] = usage_sink.get("calls", 0) + 1
        return body["choices"][0]["message"]
    raise RuntimeError("unreachable")


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates ```json fences)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
