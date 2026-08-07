"""The headline benchmark: agents consuming the RAG, compared across system configs.

Retrieval is used by agents, so the number that matters is measured the way agents
use it. Every gold item is a **request a lawyer makes of the assistant**; each runs
through an **agent config** — a (retrieval preset, tool allowlist, mode) triple — and
succeeds iff an LLM equivalence judge accepts the answer against the verified gold
answer. Results are sliced by ``meta.anchor`` (does the request cite an identifier,
an entity, an amount, or nothing), which is what the lexical and identifier legs are
supposed to serve — rather than by an invented query kind.

The config matrix separates the claims that usually get conflated:

| config              | retrieval    | tools        | answers                          |
|---------------------|--------------|--------------|----------------------------------|
| oracle              | none (given) | none         | the ceiling: what retrieval cannot fix |
| classic_rag         | full         | none (stuff) | is an agent worth it at all      |
| agent_naive         | naive_dense  | search only  | agentic RAG on a generic store   |
| agent_hybrid        | hybrid_rrf   | search only  | agentic on the best generic      |
| agent_full_search   | full         | search only  | our retrieval, minimal agency    |
| agent_full_filters  | full         | + filters    | do filters earn their place      |
| agent_full_tools    | full         | everything   | the shipped system               |

Rows 2→6 hold the agent loop constant and vary what we built, so the spread *is* the
system's contribution under agentic use. Per-query success feeds an exact McNemar
test against ``agent_full_tools`` and every rate carries a Wilson interval, because
at n=200 differences below ~4-5 points are not resolvable and must not be ranked.
An optional wall probe runs the full-tools agent under an outsider principal.

Cost is reported as tokens, summed from each run's own usage — the gateway's
workspace-wide spend counter cannot attribute cost to a config.

Note ``classic_rag`` is a one-shot, *excerpt-only* baseline: it sees the top-k
excerpts, while an agent reads whole documents via ``get_document``. Its gap to the
agent rows therefore mixes agency with context budget and must not be read as the
value of agency alone.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.benchmark import gateway, metrics
from knowledge_index.benchmark.presets import apply_preset
from knowledge_index.config import AppConfig

OUTSIDER_PRINCIPAL = "user:benchmark-outsider"

SEARCH_ONLY: tuple[str, ...] = ("search_semantic", "get_document")
WITH_FILTERS: tuple[str, ...] = (*SEARCH_ONLY, "search_filter")

#: name → (mode, retrieval preset, tool allowlist); None = the whole MCP surface
AGENT_CONFIGS: dict[str, dict] = {
    # The ceiling: gold documents handed in, retrieval removed from the equation.
    # Whatever it misses cannot be fixed by better retrieval, so the gap between it
    # and the shipped system is exactly retrieval's share of the error budget.
    "oracle": {"mode": "oracle", "preset": "full", "tools": None},
    "classic_rag": {"mode": "classic", "preset": "full", "tools": None},
    "agent_naive": {"mode": "agentic", "preset": "naive_dense", "tools": SEARCH_ONLY},
    "agent_hybrid": {"mode": "agentic", "preset": "hybrid_rrf", "tools": SEARCH_ONLY},
    "agent_hybrid_rerank": {
        "mode": "agentic",
        "preset": "hybrid_rrf_rerank",
        "tools": SEARCH_ONLY,
    },
    "agent_full_search": {"mode": "agentic", "preset": "full", "tools": SEARCH_ONLY},
    "agent_full_filters": {"mode": "agentic", "preset": "full", "tools": WITH_FILTERS},
    "agent_full_tools": {"mode": "agentic", "preset": "full", "tools": None},
}

#: the default matrix; agent_hybrid_rerank joins on demand (--configs all)
DEFAULT_CONFIGS: tuple[str, ...] = (
    "oracle",
    "classic_rag",
    "agent_naive",
    "agent_hybrid",
    "agent_full_search",
    "agent_full_filters",
    "agent_full_tools",
)

REFERENCE_CONFIG = "agent_full_tools"

_JUDGE_SYSTEM = (
    "You grade whether a candidate answer to a question is correct, given the reference "
    "answer. Accept semantically equivalent phrasings (e.g. different date or name "
    "formats, extra context). Respond with JSON only: "
    '{"verdict": "correct" | "incorrect"}'
)


def resolve_configs(spec: str) -> tuple[str, ...]:
    """Resolve ``--configs``: 'default', 'all', or a comma-separated config list."""
    if spec == "default":
        return DEFAULT_CONFIGS
    if spec == "all":
        return tuple(AGENT_CONFIGS)
    names = tuple(name.strip() for name in spec.split(",") if name.strip())
    unknown = [name for name in names if name not in AGENT_CONFIGS]
    if unknown:
        raise KeyError(f"unknown config(s) {unknown}; known: {sorted(AGENT_CONFIGS)}")
    return names


def load_gold(gold_path: str | Path) -> list[dict]:
    return [
        json.loads(line)
        for line in Path(gold_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sample_gold(gold: list[dict], limit: int | None, *, seed: int = 42) -> list[dict]:
    """Sample requests evenly across anchor types (seeded, deterministic)."""
    if limit is None or limit >= len(gold):
        return gold
    rng = random.Random(seed)
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for item in gold:
        by_kind[(item.get("meta") or {}).get("anchor", "none")].append(item)
    for group in by_kind.values():
        rng.shuffle(group)
    kinds = sorted(by_kind)
    picked: list[dict] = []
    index = 0
    while len(picked) < limit and any(by_kind[kind] for kind in kinds):
        kind = kinds[index % len(kinds)]
        if by_kind[kind]:
            picked.append(by_kind[kind].pop())
        index += 1
    return picked


def _parse_verdict(payload: dict) -> bool:
    return str(payload.get("verdict", "incorrect")).strip().lower() == "correct"


def judge_answer(
    question: str,
    candidate: str,
    reference: str,
    config: AppConfig,
    model: str,
    *,
    usage_sink: dict | None = None,
) -> bool:
    if not candidate.strip():
        return False
    user = f"Question: {question}\nReference answer: {reference}\nCandidate answer: {candidate}"
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        # Reasoning models spend part of the budget on hidden reasoning tokens; a small
        # cap left no room for the verdict JSON -> every answer scored "incorrect".
        message = gateway.complete(config, model, messages, max_tokens=2000, usage_sink=usage_sink)
        return _parse_verdict(gateway.extract_json(message.get("content") or ""))
    except Exception:
        return False


def _names_a_gold_document(answer: str, gold_paths: list[str]) -> bool:
    """Does the answer actually identify one of the gold documents?

    Matches the filename stem (``fund-v-lpa-draft`` from
    ``…/fund-v-lpa-draft.docx``), tolerating the separator drift a model applies
    when it prose-ifies a filename ("Fund V LPA draft").
    """
    if not answer or not answer.strip():
        return False
    haystack = re.sub(r"[^a-z0-9]+", " ", answer.casefold())
    for path in gold_paths:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        needle = re.sub(r"[^a-z0-9]+", " ", stem.casefold()).strip()
        if needle and needle in haystack:
            return True
    return False


def context_recall(retrieved: set[str], primary: set[str]) -> float:
    """Did the agent actually pull the document that answers the request?

    Measured against the *primary* gold document only. Against the flat gold set
    this read far below the judge's pass rate for a mechanical reason, not a real
    one: gold averages ~2.5 documents but only one of them answers the request, so
    an agent that found that one, answered correctly, and stopped looking scored
    0.4 here while passing the judge.
    """
    if not primary:
        return 0.0
    return round(len(retrieved & primary) / len(primary), 4)


def apply_grading(runs: list[dict], gold: list[dict]) -> list[dict]:
    """Re-derive the graded fields of finished runs from the gold file.

    Grading lives in the gold, not in the run, so it is resolved at aggregation
    time. That keeps a checkpoint written before a grading change usable instead
    of forcing a re-run of work that is already paid for.
    """
    by_id = {item["id"]: item for item in gold}
    for run in runs:
        item = by_id.get(run["id"])
        if item is None:
            continue
        primary, _ = metrics.graded_gold(item)
        run["primary_paths"] = sorted(primary)
        run["context_recall"] = context_recall(set(run["retrieved_paths"]), primary)
    return runs


def aggregate_config(runs: list[dict]) -> dict:
    """Mean the per-run scores of one config. Pure.

    Runs the provider refused (``blocked``) are excluded from every rate: no answer was
    generated, so scoring them as wrong would report a provider policy decision as a
    system quality number. They are counted separately so a config whose questions were
    half eaten by a safety filter cannot masquerade as a clean result.
    """
    attempted = len(runs)
    if not attempted:
        return {"queries": 0}
    blocked = [run for run in runs if run.get("blocked")]
    runs = [run for run in runs if not run.get("blocked")]
    n = len(runs)
    if not n:
        return {"queries": attempted, "scored": 0, "blocked": len(blocked)}
    anchored = [run for run in runs if run.get("anchor", "none") != "none"]
    unanchored = [run for run in runs if run.get("anchor", "none") == "none"]
    successes = sum(1 for run in runs if run["success"])
    summary = {
        "queries": attempted,
        "scored": n,
        "blocked": len(blocked),
        "success_rate": round(successes / n, 4),
        "success_ci95": metrics.wilson_interval(successes, n),
        "context_recall": round(sum(run["context_recall"] for run in runs) / n, 4),
        # Fraction of runs that surfaced at least ONE gold document. Fairer to an
        # agent than fractional recall: gold often spans several documents and an
        # agent that answers from the first one correctly stops looking.
        "any_gold_surfaced": round(
            sum(
                1
                for run in runs
                if set(run["retrieved_paths"]) & set(run["gold_paths"])
            )
            / n,
            4,
        ),
        "avg_tool_calls": round(sum(run["tool_calls"] for run in runs) / n, 2),
        "avg_llm_calls": round(sum(run["llm_calls"] for run in runs) / n, 2),
        "avg_wall_seconds": round(sum(run["wall_seconds"] for run in runs) / n, 2),
        "total_tokens": sum(run["usage"]["total_tokens"] for run in runs),
    }
    if anchored:
        correct = sum(1 for run in anchored if run["success"])
        summary["anchored_accuracy"] = round(correct / len(anchored), 4)
        summary["anchored_ci95"] = metrics.wilson_interval(correct, len(anchored))
        summary["anchored"] = len(anchored)
    if unanchored:
        correct = sum(1 for run in unanchored if run["success"])
        summary["unanchored_accuracy"] = round(correct / len(unanchored), 4)
        summary["unanchored_ci95"] = metrics.wilson_interval(correct, len(unanchored))
        summary["unanchored"] = len(unanchored)
    return summary


def _run_one(
    item: dict,
    config_name: str,
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    agent_model: str,
    judge_model: str,
    max_steps: int,
    principal: str | None = None,
) -> dict:
    from knowledge_index.benchmark import agent
    from knowledge_index.retrieval import RetrievalService

    spec = AGENT_CONFIGS[config_name]
    ablated = apply_preset(config, spec["preset"])
    who = principal or item["principals"][0]
    primary, _ = metrics.graded_gold(item)
    started = time.monotonic()
    try:
        with session_factory() as session:
            service = RetrievalService(session, ablated)
            if spec["mode"] == "oracle":
                produced = agent.run_oracle(
                    item["query"],
                    agent.gold_document_texts(session, item["gold_paths"]),
                    ablated,
                    agent_model,
                    primary=primary,
                )
            elif spec["mode"] == "classic":
                produced = agent.run_classic_rag(item["query"], who, service, ablated, agent_model)
            else:
                produced = agent.run_agentic(
                    item["query"],
                    who,
                    service,
                    ablated,
                    agent_model,
                    allowed_tools=set(spec["tools"]) if spec["tools"] is not None else None,
                    max_steps=max_steps,
                )
    except gateway.ProviderRefused as refusal:
        # The provider never produced an answer, so there is nothing to judge. Recorded
        # and excluded from the rates rather than counted wrong — see aggregate_config.
        return {
            "id": item["id"],
            "kind": item["kind"],
            "anchor": (item.get("meta") or {}).get("anchor", "none"),
            "config": config_name,
            "query": item["query"],
            "reference": (item.get("meta") or {}).get("answer"),
            "answer": "",
            "success": False,
            "blocked": True,
            "blocked_reason": refusal.finish_reason,
            "blocked_model": refusal.model,
            "context_recall": 0.0,
            "gold_paths": item["gold_paths"],
            "primary_paths": sorted(primary),
            "retrieved_paths": [],
            "tool_calls": 0,
            "llm_calls": 0,
            "trajectory": [],
            "usage": {"agent": {}, "judge": {}, "total_tokens": 0},
            "wall_seconds": round(time.monotonic() - started, 2),
            "principal": who,
        }
    recall = context_recall(produced.retrieved_paths, primary)
    judge_usage: dict = {}
    # Every gold item is a request the user made of the assistant, so success is
    # always "did it answer correctly" — judged against the verified gold answer.
    reference = (item.get("meta") or {}).get("answer", "")
    blocked_judge = ""
    try:
        success = judge_answer(
            item["query"], produced.answer, reference, config, judge_model,
            usage_sink=judge_usage,
        )
    except gateway.ProviderRefused as refusal:
        # The agent answered; the judge was refused. The run is unscoreable for the same
        # reason a refused agent is, so it is blocked rather than counted wrong — the
        # agent's own work (retrieval, tool calls, tokens) is still recorded below.
        success = False
        blocked_judge = refusal.finish_reason
    agent_tokens = produced.usage.get("total_tokens", 0)
    return {
        "id": item["id"],
        "kind": item["kind"],
        "anchor": (item.get("meta") or {}).get("anchor", "none"),
        "config": config_name,
        "query": item["query"],
        "reference": (item.get("meta") or {}).get("answer"),
        "answer": produced.answer,
        "success": success,
        **({"blocked": True, "blocked_reason": f"judge:{blocked_judge}"} if blocked_judge else {}),
        "context_recall": recall,
        "gold_paths": item["gold_paths"],
        "primary_paths": sorted(primary),
        "retrieved_paths": sorted(produced.retrieved_paths),
        "tool_calls": produced.tool_calls,
        "llm_calls": produced.llm_calls,
        "trajectory": produced.trajectory,
        "usage": {
            "agent": produced.usage,
            "judge": judge_usage,
            "total_tokens": agent_tokens + judge_usage.get("total_tokens", 0),
        },
        "wall_seconds": round(time.monotonic() - started, 2),
        "principal": who,
    }


def wall_probe(
    gold: list[dict],
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    agent_model: str,
    judge_model: str,
    max_steps: int,
    sample: int = 10,
    seed: int = 42,
    concurrency: int = 8,
) -> dict:
    """Run the full-tools agent as an outsider: every trajectory must surface nothing.

    Stronger than a raw-query probe — the agent may try every tool it has, and the
    ACL must hold across all of them.
    """
    rng = random.Random(seed)
    probes = gold if len(gold) <= sample else rng.sample(gold, sample)

    def _probe(item: dict) -> dict:
        return _run_one(
            item,
            REFERENCE_CONFIG,
            session_factory,
            config,
            agent_model=agent_model,
            judge_model=judge_model,
            max_steps=max_steps,
            principal=OUTSIDER_PRINCIPAL,
        )

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        probe_runs = list(pool.map(_probe, probes))
    leaks = [item["id"] for item, run in zip(probes, probe_runs) if run["retrieved_paths"]]
    return {"probed": len(probes), "leaks": leaks, "clean": not leaks}


def evaluate_agentic(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    gold_path: str | Path,
    *,
    configs: tuple[str, ...] = DEFAULT_CONFIGS,
    agent_model: str,
    judge_model: str,
    limit: int | None = None,
    max_steps: int = 12,
    seed: int = 42,
    probe_walls: bool = False,
    concurrency: int = 8,
    checkpoint_dir: str | Path | None = None,
) -> dict:
    """Run the agentic matrix; compare configs on per-query success with CIs.

    Queries fan out over a bounded thread pool *within* each config (every query
    opens its own session and tool suite, and vLLM/gateway backends batch
    concurrent requests); configs run sequentially so the before/after LiteLLM
    per-config token accounting stays attributable.

    ``checkpoint_dir`` makes the matrix crash-safe: each config's runs are written
    to ``<dir>/<config>.json`` on completion and loaded (same gold sample
    fingerprint) instead of re-run — a crash costs at most the config in flight.
    """
    import hashlib

    gold = sample_gold(load_gold(gold_path), limit, seed=seed)
    fingerprint = hashlib.sha256(
        json.dumps(sorted(item["id"] for item in gold)).encode()
    ).hexdigest()[:16]
    ckpt = Path(checkpoint_dir) if checkpoint_dir else None
    if ckpt:
        ckpt.mkdir(parents=True, exist_ok=True)

    runs: dict[str, list[dict]] = {}
    for name in configs:
        ckpt_file = ckpt / f"{name}.json" if ckpt else None
        if ckpt_file and ckpt_file.is_file():
            cached = json.loads(ckpt_file.read_text(encoding="utf-8"))
            if cached.get("gold_fingerprint") == fingerprint:
                runs[name] = apply_grading(cached["runs"], gold)
                continue  # stale checkpoints (different sample) are simply re-run

        def _one(item: dict, config_name: str = name) -> dict:
            try:
                return _run_one(
                    item,
                    config_name,
                    session_factory,
                    config,
                    agent_model=agent_model,
                    judge_model=judge_model,
                    max_steps=max_steps,
                )
            except Exception as exc:
                # A query that still fails after the gateway retries scores as a
                # failure with the fault recorded — it must not kill the config.
                return {
                    "id": item["id"],
                    "kind": item["kind"],
                    "anchor": (item.get("meta") or {}).get("anchor", "none"),
                    "config": config_name,
                    "query": item["query"],
                    "reference": (item.get("meta") or {}).get("answer"),
                    "answer": "",
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "context_recall": 0.0,
                    "gold_paths": item["gold_paths"],
                    "retrieved_paths": [],
                    "tool_calls": 0,
                    "llm_calls": 0,
                    "trajectory": [],
                    "usage": {"agent": {}, "judge": {}, "total_tokens": 0},
                    "wall_seconds": 0.0,
                    "principal": item["principals"][0],
                }

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            # Graded through the same path as a cached checkpoint, so a fresh run
            # and a resumed one cannot report different numbers — and so the rows
            # written by the failure fallback carry the grading fields too.
            runs[name] = apply_grading(list(pool.map(_one, gold)), gold)
        if ckpt_file:
            ckpt_file.write_text(
                json.dumps(
                    {"gold_fingerprint": fingerprint, "runs": runs[name]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    # Cost is reported as TOKENS, summed from each run's own usage — exact and
    # attributable. The gateway's /global/spend counter cannot be used here: it is
    # workspace-wide, so with any concurrent traffic each config's "spend delta"
    # measures the wall clock, not the config (two configs once reported identical
    # spend to six decimals while consuming 8.7M and 10.3M tokens). Multiply tokens
    # by the deployment's contracted rate to get money.
    summary = {name: aggregate_config(config_runs) for name, config_runs in runs.items()}

    # Exact McNemar on paired per-query success against the shipped system. Paired
    # binary outcomes on the same queries are exactly what McNemar is for; only
    # discordant pairs carry signal, and the p-value states plainly whether a
    # reported win is resolvable at this sample size.
    comparison: dict[str, dict] = {}
    if REFERENCE_CONFIG in runs:
        reference_by_id = {run["id"]: bool(run["success"]) for run in runs[REFERENCE_CONFIG]}
        ids = sorted(reference_by_id)
        for name, config_runs in runs.items():
            if name == REFERENCE_CONFIG:
                continue
            treatment_by_id = {run["id"]: bool(run["success"]) for run in config_runs}
            comparison[name] = metrics.mcnemar(
                [treatment_by_id.get(query_id, False) for query_id in ids],
                [reference_by_id[query_id] for query_id in ids],
            )

    report: dict = {
        "gold_path": str(gold_path),
        "configs": list(configs),
        "queries": len(gold),
        "summary": summary,
        "comparison_vs_reference": {"reference": REFERENCE_CONFIG, "deltas": comparison},
        "runs": runs,
    }
    if probe_walls:
        report["wall_probe"] = wall_probe(
            gold,
            session_factory,
            config,
            agent_model=agent_model,
            judge_model=judge_model,
            max_steps=max_steps,
            seed=seed,
            concurrency=concurrency,
        )
    return report


def render_agentic_markdown(report: dict) -> str:
    """The agentic comparison as a markdown table."""
    # any-gold is the headline retrieval signal, not fractional context recall: it
    # correlates r=0.73 with task success where fractional recall manages r=0.48,
    # and fractional recall correlates r=0.36 with GOLD-SET SIZE — a labelling
    # artifact. Across gold sizes 1→5 success is flat (0.913→0.944) while fractional
    # recall falls 0.31 and any-gold rises to 0.986.
    lines = [
        "| config | success (95% CI) | anchored | unanchored | any-gold "
        "| vs full-tools (McNemar) | tool calls | ctx recall (diag.) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    deltas = report["comparison_vs_reference"]["deltas"]
    for name in report["configs"]:
        row = report["summary"][name]
        versus = deltas.get(name)
        if versus is None:
            comparison = "— (reference)"
        else:
            verdict = "SIGNIFICANT" if versus.get("significant") else "not resolvable"
            comparison = (
                f"{versus['net']:+d} net ({versus['treatment_only']}/"
                f"{versus['reference_only']} discordant), p={versus['p_value']} — {verdict}"
            )
        low, high = row.get("success_ci95", [None, None])
        interval = f" [{low}, {high}]" if low is not None else ""
        # n is the SCORED count, not the attempted one: rates below have blocked runs
        # removed, and a bare success rate would otherwise hide a shrinking denominator.
        scored = f" (n={row.get('scored')}"
        scored += f", {row['blocked']} blocked)" if row.get("blocked") else ")"
        lines.append(
            f"| {name} | {row.get('success_rate')}{interval}{scored} "
            f"| {row.get('anchored_accuracy', '—')} "
            f"| {row.get('unanchored_accuracy', '—')} | {row.get('any_gold_surfaced', '—')} "
            f"| {comparison} | {row.get('avg_tool_calls')} "
            f"| {row.get('context_recall')} |"
        )
    probe = report.get("wall_probe")
    if probe:
        lines.append("")
        lines.append(
            f"Wall probe (outsider agent, full tools): {probe['probed']} probed → "
            f"{'clean' if probe['clean'] else 'LEAKS: ' + ', '.join(probe['leaks'])}"
        )
    return "\n".join(lines)
