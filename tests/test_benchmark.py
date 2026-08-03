"""Benchmark harness: offline unit coverage + one live end-to-end run.

The unit tests need no database, models, or docker stack — the metric core takes an
injectable search function and the corpus/gold builders are pure filesystem work.
The single ``@pytest.mark.integration`` test packs a tiny corpus, ingests it through
the real pipeline, and asserts the full system beats the naive-dense baseline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import pytest
from docx import Document as WordDocument

from knowledge_index.benchmark import baselines, metrics
from knowledge_index.benchmark.gold import derive_gold, write_gold
from knowledge_index.benchmark.harness import run_queries
from knowledge_index.benchmark.task_corpus import build_task_corpus
from knowledge_index.config import AppConfig
from tests.conftest import TEST_EMBEDDING_MODEL, TEST_LLM_MODEL


# --------------------------------------------------------------------------- metrics


def test_metrics_match_hand_computed_values() -> None:
    gold = {"a", "b"}
    ranked_covers = [{"a"}, set(), {"b"}]  # rank1 a, rank2 miss, rank3 b
    assert metrics.recall_at_k(ranked_covers, gold, 1) == 0.5
    assert metrics.recall_at_k(ranked_covers, gold, 5) == 1.0
    assert metrics.precision_at_k(ranked_covers, gold, 1) == 1.0
    assert metrics.reciprocal_rank(ranked_covers, gold) == 1.0
    # dcg = 1/log2(2) + 1/log2(4) = 1.5 ; ideal = 1/log2(2) + 1/log2(3)
    assert metrics.ndcg_at_k(ranked_covers, gold, 10) == pytest.approx(0.9197, abs=1e-3)


def test_metrics_reward_earlier_hits() -> None:
    gold = {"a"}
    early = metrics.ndcg_at_k([{"a"}, set(), set()], gold, 10)
    late = metrics.ndcg_at_k([set(), set(), {"a"}], gold, 10)
    assert early > late == pytest.approx(0.5, abs=1e-3)


def test_aggregate_splits_by_kind() -> None:
    scores = [
        metrics.QueryScore.compute("q1", "factoid", [{"a"}], {"a"}),
        metrics.QueryScore.compute("q2", "instruction_working_set", [set(), {"b"}], {"b"}),
    ]
    report = metrics.aggregate(scores)
    assert report["overall"]["queries"] == 2
    assert set(report["by_kind"]) == {"factoid", "instruction_working_set"}
    assert report["by_kind"]["factoid"]["recall"]["@1"] == 1.0


# -------------------------------------------------------------------------- baselines


def test_naive_dense_zeroes_the_smart_legs_without_mutating_input() -> None:
    config = AppConfig()
    before = config.retrieval.model_dump()
    ablated = baselines.apply_baseline(config, "naive_dense")
    assert ablated.retrieval.weight_lexical == 0.0
    assert ablated.retrieval.weight_identifier == 0.0
    assert ablated.retrieval.weight_semantic == 1.0
    assert ablated.retrieval.collapse_per_document is False
    assert ablated.retrieval.rerank_enabled is False
    # the original config is untouched (deep copy)
    assert config.retrieval.model_dump() == before
    assert config.retrieval.weight_identifier == 1.5


def test_full_baseline_is_the_shipped_defaults() -> None:
    config = AppConfig()
    assert baselines.apply_baseline(config, "full").retrieval.model_dump() == (
        config.retrieval.model_dump()
    )


def test_unknown_baseline_fails_loud() -> None:
    with pytest.raises(KeyError):
        baselines.apply_baseline(AppConfig(), "typo")


# ---------------------------------------------------------------- corpus + gold build


def _write_task(task_dir: Path, *, title: str, marker: str, filler: str) -> None:
    """One upstream-shaped task: task.json + a docx (holds the marker) + an email."""
    scenario = task_dir / "scenario-01"
    documents = scenario / "documents"
    documents.mkdir(parents=True)
    document = WordDocument()
    document.add_paragraph(f"{title} playbook")
    document.add_paragraph(f"The Secured Party is {marker} acting as Administrative Agent.")
    document.save(documents / "playbook.docx")
    email = EmailMessage()
    email["Subject"] = f"{title} engagement"
    email["From"] = "partner@firm.invalid"
    email["To"] = "associate@firm.invalid"
    email.set_content(f"Please prepare the {title}. {filler}")
    (documents / "engagement.eml").write_bytes(email.as_bytes())
    (scenario / "task.json").write_text(
        json.dumps(
            {
                "title": f"Draft {title}",
                "instructions": (
                    f"Using the provided template, draft the {title} using the reference "
                    "files.\n\n### Output:\nresult.docx"
                ),
                "criteria": [
                    {
                        "id": "C-001",
                        "title": f"{marker} identified in preamble",
                        "match_criteria": (
                            f"PASS if the preamble identifies the Secured Party as "
                            f"'{marker}'. FAIL otherwise."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _mini_task_set(root: Path) -> Path:
    area = root / "tasks" / "contracts" / "banking"
    _write_task(
        area / "account-control-agreement-first-draft",
        title="Account Control Agreement",
        marker="Pinnacle Capital Partners, LLC",
        filler="It is a Delaware limited liability company.",
    )
    _write_task(
        area / "credit-support-annex-first-draft",
        title="Credit Support Annex",
        marker="Meridian Engineered Systems, Inc.",
        filler="It is a Virginia corporation.",
    )
    return root


def _write_flat_task(task_dir: Path, *, title: str) -> None:
    """Write the flat task/task.json layout used by current upstream areas."""
    documents = task_dir / "documents"
    documents.mkdir(parents=True)
    document = WordDocument()
    document.add_heading(title, 0)
    document.add_paragraph(f"Reference material for {title}.")
    document.save(documents / "reference.docx")
    (task_dir / "task.json").write_text(
        json.dumps({"title": title, "instructions": f"Review {title}.", "criteria": []}),
        encoding="utf-8",
    )


def test_build_corpus_shapes_a_firm_tree_and_is_deterministic(tmp_path: Path) -> None:
    source = _mini_task_set(tmp_path / "tasks")
    summary = build_task_corpus(tmp_path / "out", source, areas=["contracts/banking"])

    assert summary["matters"] == 2
    assert summary["scenarios"] == 2
    assert summary["documents"] == 4
    assert summary["principals"] == ["group:contracts-banking-team"]
    # the connector-facing ACL map covers every packed document, under Mandate/
    acl = json.loads(Path(summary["acl_by_path"]).read_text(encoding="utf-8"))
    assert len(acl) == 4
    assert all(path.startswith("Mandate/") for path in acl)

    repeat = build_task_corpus(tmp_path / "out2", source, areas=["contracts/banking"])
    assert repeat["content_hash"] == summary["content_hash"]


def test_build_corpus_supports_flat_task_layout(tmp_path: Path) -> None:
    source = tmp_path / "tasks"
    area = source / "tasks" / "banking-finance"
    _write_flat_task(area / "draft-fee-letter", title="Fee Letter")
    _write_flat_task(area / "review-credit-agreement", title="Credit Agreement")

    summary = build_task_corpus(tmp_path / "out", source, areas=["banking-finance"])

    assert summary["matters"] == 2
    assert summary["scenarios"] == 2
    assert summary["documents"] == 2
    assert summary["principals"] == ["group:banking-finance-team"]


def test_firm_layout_uses_the_injected_party_resolver(tmp_path: Path) -> None:
    """The firm builder names each matter via the injected resolver and groups matters
    under one client folder + one per-client ACL — the seam the LLM resolver plugs into."""
    source = _mini_task_set(tmp_path / "task_set")
    seen: list[int] = []

    def resolver(scenarios):  # both matters belong to one client, distinct counterparties
        seen.append(len(scenarios))
        return [("Northwind", "Acme"), ("Northwind", "Globex")]

    summary = build_task_corpus(
        tmp_path / "out",
        source,
        areas=["contracts/banking"],
        layout="firm",
        resolve_parties=resolver,
    )

    assert seen == [2]  # the resolver saw exactly the selected scenarios
    assert summary["layout"] == "firm"
    assert summary["clients"] == 1
    assert summary["client_names"] == ["Northwind"]
    assert summary["matters"] == 2
    assert summary["principals"] == ["group:northwind"]
    acl = json.loads(Path(summary["acl_by_path"]).read_text(encoding="utf-8"))
    assert acl and all(path.startswith("Clients/Northwind/") for path in acl)
    # each matter is "<instrument> — <counterparty>"; pairing follows the seeded order
    titles = {r["matter_title"] for r in _read_scenarios(summary)}
    assert {t.split(" — ")[0] for t in titles} == {
        "Account Control Agreement",
        "Credit Support Annex",
    }
    assert {t.split(" — ")[1] for t in titles} == {"Acme", "Globex"}


def test_firm_naming_resolver_rejected_for_flat_layout(tmp_path: Path) -> None:
    source = _mini_task_set(tmp_path / "task_set")
    with pytest.raises(ValueError, match="firm layout"):
        build_task_corpus(
            tmp_path / "out",
            source,
            areas=["contracts/banking"],
            layout="flat",
            resolve_parties=lambda scenarios: [("X", "Y")],
        )


def test_firm_layout_applies_a_curated_structure_manifest(tmp_path: Path) -> None:
    """A structure manifest is authoritative: its client / matter number / title are used
    verbatim, matters group under the shared client, and a '/' in a title is folder-safe."""
    source = _mini_task_set(tmp_path / "task_set")
    structure = {
        "matters": {
            "contracts/banking/account-control-agreement-first-draft/scenario-01": {
                "client": "Northwind Capital",
                "matter_no": "NC-001",
                "counterparty": "Acme",
                "matter_title": "ISDA / Credit Support Annex — Acme",
            },
            "contracts/banking/credit-support-annex-first-draft/scenario-01": {
                "client": "Northwind Capital",
                "matter_no": "NC-002",
                "counterparty": "Globex",
                "matter_title": "Account Control Agreement — Globex",
            },
        }
    }
    summary = build_task_corpus(
        tmp_path / "out", source, areas=["contracts/banking"], layout="firm", structure=structure
    )

    assert summary["clients"] == 1
    assert summary["client_names"] == ["Northwind Capital"]
    assert summary["matters"] == 2
    recs = _read_scenarios(summary)
    assert {r["matter_ref"] for r in recs} == {"NC-001", "NC-002"}
    # the '/' in the first title is sanitized to '-' so it stays one matter folder
    assert {r["matter_title"] for r in recs} == {
        "ISDA - Credit Support Annex — Acme",
        "Account Control Agreement — Globex",
    }
    acl = json.loads(Path(summary["acl_by_path"]).read_text(encoding="utf-8"))
    assert acl and all(p.startswith("Clients/Northwind Capital/NC-00") for p in acl)
    # no path segment was split by the slashed title (would appear as ".../ISDA /...")
    assert not any("/ISDA /" in p for p in acl)


def test_noise_primitives_are_deterministic_and_structural(tmp_path: Path) -> None:
    from knowledge_index.benchmark import noise

    cfg = noise.LEVELS["heavy"]
    assert noise.place("Drafts", "flat") == ""  # flat -> matter root
    assert noise.place("Drafts", "alt") == "Working Papers"  # renamed vocabulary
    assert noise.place("Drafts", "standard") == "Drafts"  # unchanged
    # a matter's style is stable across calls (seeded per matter)
    assert noise.matter_style(cfg, 42, "MCP-006") == noise.matter_style(cfg, 42, "MCP-006")

    # junk leaves an empty limbo dir and returns cruft files for the caller to wall
    matter = tmp_path / "matter"
    matter.mkdir()
    junk = noise.scatter_junk(matter, 42, "MCP-006")
    assert any(d.is_dir() and d.name.startswith("_") for d in matter.iterdir())
    assert all(j.exists() for j in junk)


def test_firm_noise_keeps_every_file_walled_and_is_deterministic(tmp_path: Path) -> None:
    """Noise may flatten/rename folders, add versions and junk — but every file stays
    walled to its own client, and the whole build is reproducible."""
    import re

    source = _mini_task_set(tmp_path / "task_set")
    summary = build_task_corpus(
        tmp_path / "out", source, areas=["contracts/banking"], layout="firm", noise_level="heavy"
    )
    assert set(summary["noise"]) == {"flat", "alt", "junk"}

    root = Path(summary["source_root"])
    acl = json.loads(Path(summary["acl_by_path"]).read_text(encoding="utf-8"))
    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    assert files and all(f in acl for f in files)  # every file (incl. junk/versions) walled
    for path, grants in acl.items():  # and walled to its OWN client — no leak
        client_slug = re.sub(r"[^a-z0-9]+", "-", path.split("/")[1].lower()).strip("-")
        assert [g["principal"] for g in grants] == [f"group:{client_slug}"]

    repeat = build_task_corpus(
        tmp_path / "out2", source, areas=["contracts/banking"], layout="firm", noise_level="heavy"
    )
    assert repeat["content_hash"] == summary["content_hash"]


def test_noise_requires_firm_layout(tmp_path: Path) -> None:
    source = _mini_task_set(tmp_path / "task_set")
    with pytest.raises(ValueError, match="firm layout"):
        build_task_corpus(
            tmp_path / "out", source, areas=["contracts/banking"], layout="flat", noise_level="light"
        )


def _read_scenarios(summary: dict) -> list[dict]:
    lines = Path(summary["scenarios_manifest"]).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_gold_derivation_emits_working_set_and_clean_factoids(tmp_path: Path) -> None:
    source = _mini_task_set(tmp_path / "tasks")
    build_task_corpus(tmp_path / "out", source, areas=["contracts/banking"])
    gold = derive_gold(tmp_path / "out")

    working = [g for g in gold if g.kind == "instruction_working_set"]
    factoid = [g for g in gold if g.kind == "factoid"]
    assert len(working) == 2
    # each working-set query targets its scenario's whole bundle and omits "### Output"
    assert all(len(g.gold_paths) == 2 for g in working)
    assert all("### Output" not in g.query for g in working)
    # the quoted party name resolves to exactly the one docx that contains it
    assert factoid, "expected at least one factoid label"
    for g in factoid:
        assert len(g.gold_paths) == 1
        assert g.gold_paths[0].endswith("playbook.docx")
        assert g.query in ("Pinnacle Capital Partners, LLC", "Meridian Engineered Systems, Inc.")

    stats = write_gold(tmp_path / "out")
    assert stats["by_kind"]["instruction_working_set"] == 2
    assert (tmp_path / "out" / "retrieval-gold.jsonl").exists()


# ------------------------------------------------------------------------- gold store


def test_freeze_makes_gold_a_committed_artifact_resolvable_by_name(tmp_path: Path) -> None:
    from knowledge_index.benchmark import store

    source = _mini_task_set(tmp_path / "tasks")
    build_task_corpus(tmp_path / "out", source, areas=["contracts/banking"])
    write_gold(tmp_path / "out")

    data_dir = tmp_path / "frozen"
    info = store.freeze(tmp_path / "out", "banking-test", data_dir=data_dir)
    assert (data_dir / "banking-test.gold.jsonl").exists()
    meta = json.loads((data_dir / "banking-test.meta.json").read_text())
    assert meta["source"].startswith("harveyai/harvey-labs")
    assert meta["corpus_config"]["content_hash"] == info["corpus_config"]["content_hash"]
    assert store.list_frozen(data_dir=data_dir) == ["banking-test"]

    by_name = store.resolve("banking-test", data_dir=data_dir)
    by_path = store.resolve(str(data_dir / "banking-test.gold.jsonl"), data_dir=data_dir)
    assert by_name == by_path
    with pytest.raises(FileNotFoundError):
        store.resolve("does-not-exist", data_dir=data_dir)


# ----------------------------------------------------------------------- measurement


def test_percentiles_and_spend_delta() -> None:
    from knowledge_index.benchmark import measure

    assert measure.percentiles([]) == {"count": 0}
    p = measure.percentiles([10.0, 20.0, 30.0, 40.0])
    assert p["count"] == 4 and p["p50"] == 30.0 and p["max"] == 40.0

    before = {"total": 0.10, "by_model": {"gpt-4.1-mini": 0.08, TEST_EMBEDDING_MODEL: 0.02}}
    after = {"total": 0.25, "by_model": {"gpt-4.1-mini": 0.20, TEST_EMBEDDING_MODEL: 0.05}}
    delta = measure.spend_delta(before, after)
    assert delta["total"] == 0.15
    assert delta["by_model"]["gpt-4.1-mini"] == 0.12

    # unavailable spend propagates rather than being estimated
    assert measure.spend_delta({"total": None, "error": "x"}, after)["total"] is None


# --------------------------------------------------------------------- harness scoring


@dataclass
class _FakeHit:
    source_paths: list[str]
    version_status: str


def test_run_queries_scores_and_flags_wall_leaks() -> None:
    gold = [
        {
            "id": "M-2026-0001/first-draft#ws",
            "kind": "instruction_working_set",
            "query": "draft the agreement",
            "gold_paths": ["a.docx", "b.docx"],
            "principals": ["group:x-team"],
            "matter_ref": "M-2026-0001",
            "practice_area": "x",
        }
    ]

    def search(query, principals, filters):
        return [
            _FakeHit(["a.docx"], "final"),
            _FakeHit(["distractor.docx"], "draft"),
            _FakeHit(["b.docx"], "final"),
        ]

    clean = run_queries(search, gold, outsider_search_fn=lambda *a: [])
    overall = clean["metrics"]["overall"]
    assert overall["recall"]["@1"] == 0.5
    assert overall["recall"]["@5"] == 1.0
    assert overall["mrr"] == 1.0
    assert clean["observations"]["final_not_draft_rate"] == 1.0
    assert clean["ethical_wall"]["clean"] is True

    leak_fn = lambda *a: [_FakeHit(["a.docx"], "final")]  # noqa: E731
    leaked = run_queries(search, gold, outsider_search_fn=leak_fn)
    assert leaked["ethical_wall"]["clean"] is False
    assert leaked["ethical_wall"]["leaks"] == ["M-2026-0001/first-draft#ws"]


# -------------------------------------------------------------------- llm gold grounding


def _proposal(**kw):
    from knowledge_index.benchmark.gold_llm import _Proposal

    kw.setdefault("kind", "llm_factoid")
    kw.setdefault("leg", "identifier")
    kw.setdefault("source_document", "term-sheet.docx")
    return _Proposal(**kw)


def test_llm_gold_grounding_drops_hallucinations_and_remaps_to_real_docs() -> None:
    from knowledge_index.benchmark.gold_llm import _ground_proposal

    scenario = {
        "principal": "group:x-team",
        "matter_ref": "M-2026-0001",
        "practice_area": "contracts/banking",
    }
    doc_texts = {
        "Mandate/m/term-sheet.docx": "the secured party is pinnacle capital partners, llc.",
        "Mandate/m/memo.docx": "pinnacle capital partners, llc acts as agent.",
        "Mandate/m/other.docx": "unrelated content about scheduling.",
    }
    paths = list(doc_texts)

    # grounded value present in two docs -> gold is BOTH, model's wrong guess ignored
    hit = _ground_proposal(
        _proposal(query="Who is the secured party?", answer="Pinnacle Capital Partners, LLC",
                  source_document="other.docx", kind="llm_question", leg="semantic"),
        paths, doc_texts, scenario, model=TEST_LLM_MODEL,
    )
    assert hit is not None
    assert hit.gold_paths == ["Mandate/m/memo.docx", "Mandate/m/term-sheet.docx"]
    assert hit.kind == "llm_question"
    assert hit.meta["needs_review"] is True
    assert hit.meta["extracted_by"] == f"{TEST_LLM_MODEL}/llm-gold-1"

    # answer that appears in no document -> dropped
    assert _ground_proposal(
        _proposal(query="charter?", answer="OCC Charter No. 99999"),
        paths, doc_texts, scenario, model=TEST_LLM_MODEL,
    ) is None

    # trivially short answer -> dropped
    assert _ground_proposal(
        _proposal(query="x", answer="a"),
        paths, doc_texts, scenario, model=TEST_LLM_MODEL,
    ) is None


# -------------------------------------------------------------------- coverage guard


def test_coverage_is_full_only_when_every_gold_doc_is_present() -> None:
    from knowledge_index.benchmark.harness import CorpusCoverageError, _coverage_summary

    full = _coverage_summary({"a.docx", "b.docx"}, {"a.docx", "b.docx", "extra.docx"})
    assert full["full"] is True and full["coverage"] == 1.0 and full["missing"] == []

    partial = _coverage_summary({"a.docx", "b.docx", "c.docx"}, {"a.docx"})
    assert partial["full"] is False
    assert partial["coverage"] == round(1 / 3, 4)
    assert set(partial["missing"]) == {"b.docx", "c.docx"}

    empty = _coverage_summary(set(), set())
    assert empty["full"] is False  # nothing to benchmark is also a failure

    err = CorpusCoverageError(partial)
    assert "coverage 33" in str(err)
    assert err.coverage["full"] is False


# ----------------------------------------------------------------- real-usage task eval


def test_context_recall_and_rubric_scoring() -> None:
    from knowledge_index.benchmark import judge
    from knowledge_index.benchmark.task_eval import context_recall

    assert context_recall({"a.docx", "x.docx"}, ["a.docx", "b.docx"]) == 0.5
    assert context_recall(set(), ["a.docx"]) == 0.0  # closed-book finds nothing
    assert context_recall({"a.docx", "b.docx"}, ["a.docx", "b.docx"]) == 1.0  # oracle
    assert judge.score_rubric(9, 12) == 0.75
    assert judge.score_rubric(0, 0) == 0.0


def test_task_run_aggregation_groups_by_mode() -> None:
    from knowledge_index.benchmark.task_eval import aggregate_task_runs

    runs = [
        {"mode": "closed_book", "pass_rate": 0.2, "context_recall": 0.0, "tool_calls": 0,
         "llm_calls": 1},
        {"mode": "agentic", "pass_rate": 0.8, "context_recall": 1.0, "tool_calls": 5,
         "llm_calls": 3},
        {"mode": "agentic", "pass_rate": 0.6, "context_recall": 0.5, "tool_calls": 3,
         "llm_calls": 2},
    ]
    summary = aggregate_task_runs(runs)
    assert summary["closed_book"]["tasks"] == 1
    assert summary["agentic"]["tasks"] == 2
    assert summary["agentic"]["rubric_pass_rate"] == 0.7
    assert summary["agentic"]["context_recall"] == 0.75
    assert summary["agentic"]["avg_tool_calls"] == 4.0


def test_tool_suite_exposes_the_real_mcp_surface() -> None:
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from knowledge_index.benchmark.agent import ToolSuite
    from knowledge_index.mcp_server import create_mcp_server

    engine = create_engine("sqlite://")
    service = SimpleNamespace(session=sessionmaker(engine)(), config=AppConfig())
    suite = ToolSuite(service, "group:x")

    # one source of truth: the benchmark exposes exactly the real MCP server's tools
    server = create_mcp_server(sessionmaker(engine), AppConfig)
    expected = {tool.name for tool in asyncio.run(server.list_tools())}
    names = {spec["function"]["name"] for spec in suite.specs()}
    assert names == expected
    assert {"search_semantic", "get_document", "list_matters", "resolve_entity"} <= names
    for spec in suite.specs():
        assert spec["type"] == "function" and "parameters" in spec["function"]


# ------------------------------------------------------------------------------- qa eval


def test_qa_aggregation_verdict_and_gold_filtering(tmp_path: Path) -> None:
    from knowledge_index.benchmark.qa_eval import _parse_verdict, aggregate_qa, load_qa_gold

    assert _parse_verdict({"verdict": "correct"}) is True
    assert _parse_verdict({"verdict": "incorrect"}) is False
    assert _parse_verdict({}) is False

    runs = [
        {"correct": True, "context_recall": 1.0, "tool_calls": 3, "llm_calls": 2},
        {"correct": False, "context_recall": 0.0, "tool_calls": 1, "llm_calls": 1},
    ]
    agg = aggregate_qa(runs)
    assert agg["questions"] == 2 and agg["answer_accuracy"] == 0.5 and agg["context_recall"] == 0.5

    gold = tmp_path / "g.jsonl"
    gold.write_text(
        "\n".join(
            [
                json.dumps({"kind": "llm_question", "query": "When?", "gold_paths": ["a"],
                            "principals": ["g"], "meta": {"answer": "2025"}}),
                json.dumps({"kind": "factoid", "query": "x", "gold_paths": ["b"],
                            "principals": ["g"]}),  # not a QA kind
                json.dumps({"kind": "llm_question", "query": "noanswer", "gold_paths": ["c"],
                            "principals": ["g"], "meta": {}}),  # QA kind but no answer
            ]
        ),
        encoding="utf-8",
    )
    qa = load_qa_gold(gold)
    assert len(qa) == 1 and qa[0]["query"] == "When?"


# ------------------------------------------------------------------------- integration


@pytest.mark.integration
def test_benchmark_runs_end_to_end_and_full_beats_naive(
    factory,
    integration_config: AppConfig,
    settle_pipeline,
    refresh_search,
    tmp_path: Path,
) -> None:
    from sqlalchemy.orm import sessionmaker

    from knowledge_index.benchmark import evaluate_ladder
    from knowledge_index.db.models import Source
    from knowledge_index.sync import LocalFilesystemSource, SyncEngine

    source = _mini_task_set(tmp_path / "tasks")
    summary = build_task_corpus(tmp_path / "out", source, areas=["contracts/banking"])
    write_gold(tmp_path / "out")
    source_root = Path(summary["source_root"])
    acl_map = json.loads(Path(summary["acl_by_path"]).read_text(encoding="utf-8"))

    assert isinstance(factory, sessionmaker)
    with factory() as session:  # type: Session
        record = Source(
            kind="local_fs",
            display_name="task-bench",
            config={"root": str(source_root), "acl_by_path": acl_map},
        )
        session.add(record)
        session.flush()
        connector = LocalFilesystemSource(
            source_root,
            acl_resolver=lambda path: acl_map[path.relative_to(source_root).as_posix()],
        )
        SyncEngine(session, record, connector).sync()
        session.commit()

    settle_pipeline(factory, integration_config)
    refresh_search(integration_config)

    gold_file = tmp_path / "out" / "retrieval-gold.jsonl"
    report = evaluate_ladder(factory, integration_config, gold_file, min_lift=0.0)
    assert report["gate"]["walls_clean"] is True
    assert report["gate"]["corpus_full"] is True
    assert report["runs"]["full"]["corpus"]["full"] is True
    full = report["comparison"]["full"]["recall@10"]
    naive = report["comparison"]["naive_dense"]["recall@10"]
    assert full is not None and naive is not None
    assert full >= naive
