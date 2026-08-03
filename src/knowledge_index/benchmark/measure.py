"""Throughput, cost, and latency measurement for benchmark runs.

Deliberately separate from retrieval quality: this answers *how fast* and *how much*,
not *how good*. Cost is read from the LiteLLM gateway's own spend accounting — the same
source ``scale_test.py`` uses — never estimated; if the gateway exposes no spend, the
number is reported as unavailable rather than guessed (matching `docs/scale-testing.md`).

``percentiles`` is pure and import-safe offline (no httpx at module load); the spend
reader lazy-imports httpx so this module can be imported without the gateway present.
"""

from __future__ import annotations

import os

from knowledge_index.config import AppConfig


def percentiles(values: list[float]) -> dict:
    """p50/p95/p99/max over a list of measurements (e.g. per-query latency in ms)."""
    if not values:
        return {"count": 0}
    ordered = sorted(values)

    def at(pct: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        index = min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1)))
        return ordered[index]

    return {
        "count": len(ordered),
        "p50": round(at(50), 2),
        "p95": round(at(95), 2),
        "p99": round(at(99), 2),
        "max": round(ordered[-1], 2),
    }


def read_litellm_spend(config: AppConfig) -> dict:
    """Total spend (USD) tracked by LiteLLM, from the aggregated endpoint.

    ``/global/spend`` returns one pre-aggregated number. The previous implementation
    read ``/spend/logs`` — every request row, client-side — which with
    ``store_prompts_in_spend_logs: true`` after a large insertion is a multi-GB
    table scan that stalls the shared appliance database for tens of minutes.
    Never read the raw log table from the benchmark.

    Returns ``{"total": float, "by_model": {}}`` or, when the gateway has no
    reachable spend endpoint, ``{"total": None, "error": ...}`` — the caller reports
    unavailability loudly instead of estimating.
    """
    import httpx


    base = config.components.litellm_url.rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY", "")  # env://LITELLM_MASTER_KEY
    headers = {"authorization": f"Bearer {key}"} if key else {}

    try:
        response = httpx.get(f"{base}/global/spend", headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        total = payload.get("spend") if isinstance(payload, dict) else None
        if total is None:
            return {"total": None, "by_model": {}, "error": f"unexpected payload: {payload!r:.200}"}
        return {"total": round(float(total), 6), "by_model": {}}
    except Exception as exc:  # gateway down, endpoint absent, or auth rejected
        return {"total": None, "by_model": {}, "error": f"{type(exc).__name__}: {exc}"}


def spend_delta(before: dict, after: dict) -> dict:
    """Cost attributable to the work between two spend snapshots."""
    if before.get("total") is None or after.get("total") is None:
        return {"total": None, "error": before.get("error") or after.get("error")}
    models = set(before.get("by_model", {})) | set(after.get("by_model", {}))
    return {
        "total": round(after["total"] - before["total"], 6),
        "by_model": {
            model: round(
                after.get("by_model", {}).get(model, 0.0)
                - before.get("by_model", {}).get(model, 0.0),
                6,
            )
            for model in sorted(models)
        },
        "requests": after.get("requests", 0) - before.get("requests", 0),
    }
