"""Real-usage task-success harness: run each consumption mode, judge, aggregate.

``aggregate_task_runs`` and ``context_recall`` are pure and offline-testable;
``evaluate_tasks`` wires the live ``RetrievalService`` + gateway. Tasks come from a
corpus's ``scenarios.jsonl`` (instruction + criteria + working-set docs). The modes are
the baseline ladder: closed_book → classic_rag → agentic → oracle.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig

MODES: tuple[str, ...] = ("closed_book", "classic_rag", "agentic", "oracle")


def load_tasks(corpus_dir: str | Path, *, limit: int | None = None) -> list:
    from knowledge_index.benchmark import agent

    scenarios = [
        json.loads(line)
        for line in (Path(corpus_dir) / "scenarios.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    tasks = [
        agent.Task(
            scenario_id=scenario["scenario_id"],
            instruction=scenario["instructions"],
            criteria=scenario["criteria"],
            gold_paths=scenario["document_paths"],
            principal=scenario["principal"],
            matter_ref=scenario["matter_ref"],
        )
        for scenario in scenarios
        if scenario.get("criteria")
    ]
    return tasks[:limit] if limit is not None else tasks


def context_recall(retrieved: set[str], gold_paths: list[str]) -> float:
    gold = set(gold_paths)
    if not gold:
        return 0.0
    return round(len(retrieved & gold) / len(gold), 4)


def _gold_texts(session: Session, task) -> dict[str, str]:
    """Converted text for each of the task's gold documents (for the oracle mode)."""
    from knowledge_index.db.models import Artifact, SourceObject

    texts: dict[str, str] = {}
    for path in task.gold_paths:
        source = session.scalar(select(SourceObject).where(SourceObject.path == path))
        if source is None:
            continue
        artifact = session.scalar(
            select(Artifact)
            .where(Artifact.content_hash == source.content_hash, Artifact.kind == "structured_json")
            .order_by(Artifact.created_at.desc())
        )
        text = (artifact.payload or {}).get("text") if artifact else None
        if text:
            texts[path] = text
    return texts


def _produce(
    mode: str,
    task,
    session_factory: sessionmaker[Session],
    config: AppConfig,
    agent_model: str,
    *,
    max_steps: int,
    work_root: str,
):
    from knowledge_index.benchmark import agent
    from knowledge_index.retrieval import RetrievalService

    if mode == "closed_book":
        return agent.run_closed_book(task, config, agent_model)
    with session_factory() as session:
        service = RetrievalService(session, config)
        if mode == "classic_rag":
            return agent.run_classic_rag(task, service, config, agent_model)
        if mode == "agentic":
            # real usage: the vendored sandbox + skills, our retrieval tools bridged in
            from knowledge_index.benchmark.sandbox_agent import run_agentic_sandbox

            return run_agentic_sandbox(
                task, service, config, agent_model, work_root=work_root, max_steps=max_steps
            )
        if mode == "oracle":
            return agent.run_oracle(task, _gold_texts(session, task), config, agent_model)
    raise ValueError(f"unknown mode {mode!r}")


def aggregate_task_runs(runs: list[dict]) -> dict:
    """Mean each metric per mode across tasks. Pure."""
    modes = sorted({run["mode"] for run in runs})
    summary: dict[str, dict] = {}
    for mode in modes:
        subset = [run for run in runs if run["mode"] == mode]
        n = len(subset)
        summary[mode] = {
            "tasks": n,
            "rubric_pass_rate": round(sum(r["pass_rate"] for r in subset) / n, 4) if n else 0.0,
            "context_recall": round(sum(r["context_recall"] for r in subset) / n, 4) if n else 0.0,
            "avg_tool_calls": round(sum(r["tool_calls"] for r in subset) / n, 2) if n else 0.0,
            "avg_llm_calls": round(sum(r["llm_calls"] for r in subset) / n, 2) if n else 0.0,
        }
    return summary


def evaluate_tasks(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    corpus_dir: str | Path,
    *,
    modes: tuple[str, ...] = MODES,
    agent_model: str,
    judge_model: str,
    limit: int | None = None,
    max_steps: int = 25,
    work_root: str = "/tmp/ki-bench",
) -> dict:
    from knowledge_index.benchmark import judge

    tasks = load_tasks(corpus_dir, limit=limit)
    runs: list[dict] = []
    for task in tasks:
        for mode in modes:
            produced = _produce(
                mode,
                task,
                session_factory,
                config,
                agent_model,
                max_steps=max_steps,
                work_root=work_root,
            )
            rubric = judge.judge_work(
                produced.work_product,
                task.criteria,
                config,
                judge_model,
                task_description=task.instruction,
            )
            runs.append(
                {
                    "scenario_id": task.scenario_id,
                    "mode": mode,
                    "pass_rate": rubric["pass_rate"],
                    "passed": rubric["passed"],
                    "total": rubric["total"],
                    "context_recall": context_recall(produced.retrieved_paths, task.gold_paths),
                    "tool_calls": produced.tool_calls,
                    "llm_calls": produced.llm_calls,
                }
            )
    return {
        "corpus_dir": str(corpus_dir),
        "tasks": len(tasks),
        "modes": list(modes),
        "summary": aggregate_task_runs(runs),
        "runs": runs,
    }
