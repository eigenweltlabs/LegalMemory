"""Question-answering benchmark: ask an agent a question, check the answer via the RAG.

Lighter than the drafting benchmark — no sandbox, no skills. Each QA task is an
``llm_question``/``llm_factoid`` gold label (produced by ``derive-llm-gold``): a
question, a verified gold answer, and the document(s) that state it. The agent
retrieves via our tools and answers in text; we score two things:

- **answer_accuracy** — an LLM equivalence judge: does the agent's answer match the
  gold answer? (dates/names get rephrased, so not string match)
- **context_recall** — did the agent retrieve the document(s) that hold the answer?

``aggregate_qa`` and the verdict parsing are pure/offline-testable; ``evaluate_qa``
wires the live RetrievalService + gateway.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig

QA_KINDS = ("llm_question", "llm_factoid")

QA_SYSTEM = (
    "You are a legal knowledge assistant. Answer the user's question using ONLY facts "
    "you retrieve from the firm's knowledge index via the tools (search_semantic is your "
    "primary tool; also list_matters, get_document, traverse). Search, open the relevant "
    "document(s), then give a SHORT, direct answer and cite the document it came from. Do "
    "not invent anything; if the index does not contain the answer, say so."
)

_JUDGE_SYSTEM = (
    "You grade whether a candidate answer to a question is correct, given the reference "
    "answer. Accept semantically equivalent phrasings (e.g. different date or name "
    "formats, extra context). Respond with JSON only: "
    '{"verdict": "correct" | "incorrect"}'
)


def load_qa_gold(gold_path: str | Path) -> list[dict]:
    rows = [
        json.loads(line)
        for line in Path(gold_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [
        row
        for row in rows
        if row.get("kind") in QA_KINDS and (row.get("meta") or {}).get("answer")
    ]


def _parse_verdict(payload: dict) -> bool:
    return str(payload.get("verdict", "incorrect")).strip().lower() == "correct"


def judge_answer(
    question: str, candidate: str, reference: str, config: AppConfig, model: str
) -> bool:
    from knowledge_index.benchmark import gateway

    if not candidate.strip():
        return False
    user = f"Question: {question}\nReference answer: {reference}\nCandidate answer: {candidate}"
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        message = gateway.complete(config, model, messages, max_tokens=50)
        return _parse_verdict(gateway.extract_json(message.get("content") or ""))
    except Exception:
        return False


def aggregate_qa(runs: list[dict]) -> dict:
    n = len(runs)
    if not n:
        return {"questions": 0}
    return {
        "questions": n,
        "answer_accuracy": round(sum(1 for r in runs if r["correct"]) / n, 4),
        "context_recall": round(sum(r["context_recall"] for r in runs) / n, 4),
        "avg_tool_calls": round(sum(r["tool_calls"] for r in runs) / n, 2),
        "avg_llm_calls": round(sum(r["llm_calls"] for r in runs) / n, 2),
    }


def evaluate_qa(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    gold_path: str | Path,
    *,
    agent_model: str,
    judge_model: str,
    limit: int | None = None,
    max_steps: int = 12,
) -> dict:
    from knowledge_index.benchmark.agent import ProducedWork, Task, run_agentic
    from knowledge_index.benchmark.measure import read_litellm_spend, spend_delta
    from knowledge_index.benchmark.task_eval import context_recall
    from knowledge_index.retrieval import RetrievalService

    gold = load_qa_gold(gold_path)
    if limit is not None:
        gold = gold[:limit]
    spend_before = read_litellm_spend(config)
    runs: list[dict] = []
    for item in gold:
        task = Task(
            scenario_id=item["id"],
            instruction=item["query"],
            criteria=[],
            gold_paths=item["gold_paths"],
            principal=item["principals"][0],
            matter_ref=item.get("matter_ref", ""),
        )
        with session_factory() as session:
            service = RetrievalService(session, config)
            produced: ProducedWork = run_agentic(
                task, service, config, agent_model, max_steps=max_steps, system=QA_SYSTEM
            )
        reference = item["meta"]["answer"]
        correct = judge_answer(item["query"], produced.work_product, reference, config, judge_model)
        runs.append(
            {
                "id": item["id"],
                "question": item["query"],
                "reference": reference,
                "answer": produced.work_product[:500],
                "correct": correct,
                "context_recall": context_recall(produced.retrieved_paths, item["gold_paths"]),
                "tool_calls": produced.tool_calls,
                "llm_calls": produced.llm_calls,
            }
        )
    cost = spend_delta(spend_before, read_litellm_spend(config))
    summary = aggregate_qa(runs)
    summary["cost_usd"] = cost.get("total")
    summary["cost_per_question"] = (
        round(cost["total"] / summary["questions"], 5)
        if cost.get("total") is not None and summary.get("questions")
        else None
    )
    return {"gold_path": str(gold_path), "summary": summary, "cost": cost, "runs": runs}
