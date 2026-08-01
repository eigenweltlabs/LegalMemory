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
) -> dict:
    """One chat completion through the gateway; returns the raw message dict.

    Retries on 429 with exponential backoff so a rate-limit blip does not turn into a
    silent FAIL verdict (the judge fans out many concurrent calls).
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
    for attempt in range(5):
        response = httpx.post(
            f"{base}/v1/chat/completions", headers=headers, json=payload, timeout=240
        )
        if response.status_code == 429 and attempt < 4:
            time.sleep(2**attempt)  # 1s, 2s, 4s, 8s
            continue
        response.raise_for_status()
        return response.json()["choices"][0]["message"]
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
