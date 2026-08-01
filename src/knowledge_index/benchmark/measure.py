"""Throughput, cost, and latency measurement for benchmark runs.

Deliberately separate from retrieval quality: this answers *how fast* and *how much*,
not *how good*. Cost is read from the LiteLLM gateway's own spend accounting — the same
source ``scale_test.py`` uses — never estimated; if the gateway exposes no spend, the
number is reported as unavailable rather than guessed (matching `docs/src/content/docs/operations/scale-testing.md`).

``percentiles`` is pure and import-safe offline (no httpx at module load); the spend
reader lazy-imports httpx so this module can be imported without the gateway present.
"""

from __future__ import annotations

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
    """Total OpenAI spend (USD) tracked by LiteLLM, overall and per model.

    Returns ``{"total": float, "by_model": {...}, "requests": n}`` or, when the
    gateway has no reachable spend endpoint, ``{"total": None, "error": ...}`` — the
    caller reports unavailability loudly instead of estimating.
    """
    import os

    import httpx

    base = config.components.litellm_url.rstrip("/")
    key = os.environ.get("LITELLM_MASTER_KEY")
    headers = {"authorization": f"Bearer {key}"} if key else {}

    # one row per request, each with `spend` and `model`; sum client-side
    try:
        response = httpx.get(f"{base}/spend/logs", headers=headers, timeout=30)
        response.raise_for_status()
        rows = response.json()
    except Exception as exc:  # gateway down, endpoint absent, or auth rejected
        return {"total": None, "by_model": {}, "error": f"{type(exc).__name__}: {exc}"}

    if not isinstance(rows, list):
        return {"total": None, "by_model": {}, "error": f"unexpected spend payload: {type(rows)}"}
    by_model: dict[str, float] = {}
    total = 0.0
    for row in rows:
        spend = float(row.get("spend") or 0.0)
        model = row.get("model") or row.get("model_group") or "unknown"
        by_model[model] = round(by_model.get(model, 0.0) + spend, 6)
        total += spend
    return {"total": round(total, 6), "by_model": by_model, "requests": len(rows)}


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
