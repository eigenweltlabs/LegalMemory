"""Direct LiteLLM-gateway chat helpers for the benchmark agent + judge.

Kept separate from ``pipeline.providers.chat_json`` (which injects a German schema
instruction) so vendored prompts — like the rubric judge — are sent verbatim.
``httpx`` is imported lazily so this module stays offline-importable.
"""

from __future__ import annotations

import json
import re

from knowledge_index.config import AppConfig


def complete(
    config: AppConfig,
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    max_tokens: int = 4000,
    usage_sink: dict | None = None,
) -> dict:
    """One chat completion through the gateway; returns the raw message dict.

    Retries on 429 with exponential backoff so a rate-limit blip does not turn into a
    silent FAIL verdict (the judge fans out many concurrent calls). When ``usage_sink``
    is given, the response's token usage is accumulated into it (for per-run cost).
    """
    import os
    import time

    import httpx

    base = config.components.litellm_url.rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY")
    payload: dict = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"authorization": f"Bearer {key}"} if key else {}
    for attempt in range(8):
        response = httpx.post(
            f"{base}/v1/chat/completions", headers=headers, json=payload, timeout=240
        )
        if response.status_code == 429 and attempt < 7:
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
