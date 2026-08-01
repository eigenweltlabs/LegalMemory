"""LLM judge for real-usage task success — reuses the upstream rubric methodology.

Per-criterion binary PASS/FAIL using the upstream ``rubric_criterion.txt`` prompt
(vendored below, MIT, from harveyai/harvey-labs ``evaluation/prompts/``), one LLM call
per criterion so each gets focused judgment — their methodology — parallelized with a
bounded pool to respect the gateway's rate limits. This replaces the earlier batched
single-call judge, which squeezed every criterion into one prompt and was neither
calibrated to the upstream numbers nor reliable.

Calls go through our LiteLLM gateway (cost stays tracked) rather than the upstream
provider SDKs; the prompt and pass/fail contract are vendored verbatim.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from knowledge_index.benchmark import gateway
from knowledge_index.config import AppConfig

# Vendored verbatim from harveyai/harvey-labs evaluation/prompts/rubric_criterion.txt (MIT).
RUBRIC_CRITERION_PROMPT = """You are evaluating a legal AI agent's work product against a specific quality criterion.

## Task
{task_description}

## Agent's Output
{agent_output}

## Criterion
**{criterion_title}**

{match_criteria}

## Instructions
Evaluate the agent's output against the criterion above.
- **PASS**: The agent's output satisfies the criterion as described
- **FAIL**: The agent's output does not satisfy the criterion as described

Respond with JSON only:
{{"verdict": "pass" | "fail", "reasoning": "Brief explanation"}}"""


def score_rubric(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 0.0


def _judge_one(
    config: AppConfig, model: str, task_description: str, agent_output: str, criterion: dict
) -> tuple[str, bool, str]:
    prompt = RUBRIC_CRITERION_PROMPT.format(
        task_description=task_description[:4000],
        agent_output=agent_output[:20000],
        criterion_title=criterion.get("title", ""),
        match_criteria=criterion.get("match_criteria", ""),
    )
    try:
        message = gateway.complete(
            config, model, [{"role": "user", "content": prompt}], max_tokens=500
        )
        data = gateway.extract_json(message.get("content") or "")
        return criterion["id"], str(data.get("verdict", "fail")).lower() == "pass", str(
            data.get("reasoning", "")
        )
    except Exception as exc:  # a judge error fails that criterion, doesn't abort the task
        return criterion["id"], False, f"judge_error: {type(exc).__name__}"


def judge_work(
    work_product: str,
    criteria: list[dict],
    config: AppConfig,
    model: str,
    *,
    task_description: str = "",
    max_workers: int = 4,
) -> dict:
    if not criteria:
        return {"pass_rate": 0.0, "passed": 0, "total": 0, "verdicts": {}, "reasons": {}}
    if not work_product.strip():
        total = len(criteria)
        return {"pass_rate": 0.0, "passed": 0, "total": total, "verdicts": {}, "reasons": {}}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(
            pool.map(
                lambda criterion: _judge_one(
                    config, model, task_description, work_product, criterion
                ),
                criteria,
            )
        )
    verdicts = {cid: ("PASS" if ok else "FAIL") for cid, ok, _ in results}
    reasons = {cid: reason for cid, _, reason in results}
    passed = sum(1 for _, ok, _ in results if ok)
    return {
        "pass_rate": score_rubric(passed, len(criteria)),
        "passed": passed,
        "total": len(criteria),
        "verdicts": verdicts,
        "reasons": reasons,
    }
