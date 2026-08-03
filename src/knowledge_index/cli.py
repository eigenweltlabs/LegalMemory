"""Operator CLI for the single-VM appliance."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from knowledge_index.config import DEFAULT_LLM_ENV
from knowledge_index.config_store import ConfigStore
from knowledge_index.permissions import configure_access
from knowledge_index.db import get_engine, init_db
from knowledge_index.db.models import ProcessingState, Source
from knowledge_index.pipeline import PipelineRunner
from knowledge_index.pipeline.runner import connector_from_source
from knowledge_index.sync import SyncEngine


def _model_flag(value: str) -> str:
    """A --*-model flag names a gateway-served model; empty means the deployment
    default LLM (KI_LLM_MODEL)."""
    return value or os.environ.get(DEFAULT_LLM_ENV, "")


def _resolve_structure(value: str) -> str:
    """A firm-structure manifest: an explicit file path, or a committed set by name."""
    path = Path(value).expanduser()
    if path.is_file():
        return str(path)
    from knowledge_index.benchmark.store import DATA_DIR

    committed = DATA_DIR / f"firm-structure-{value}.json"
    if committed.is_file():
        return str(committed)
    raise SystemExit(
        f"structure not found: {value!r} (no such file, and no committed "
        f"firm-structure-{value}.json in the benchmark package)"
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ki", description="Knowledge Index operator CLI")
    root.add_argument(
        "--config",
        default=os.environ.get("KI_CONFIG_PATH", ".ki/config.json"),
        help="non-secret config path",
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize database tables and artifact directories")

    add_source = commands.add_parser("add-source", help="add a read-only local source")
    add_source.add_argument("root")
    add_source.add_argument("--name")
    add_source.add_argument("--principal", default="group:demo-users")
    add_source.add_argument("--acl-map", help="JSON mapping of relative paths to ACL grants")

    commands.add_parser("sync", help="synchronize every active source")

    backup = commands.add_parser("backup", help="back up every store to the configured destination")
    backup.add_argument(
        "--force",
        action="store_true",
        help="take the backup even though documents are still mid-pipeline",
    )
    backup.add_argument(
        "--no-wait",
        action="store_true",
        help="return once the run is reserved instead of waiting for it to finish",
    )
    backup_key = commands.add_parser(
        "backup-key", help="show, generate or set the key backups are encrypted under"
    )
    backup_key.add_argument(
        "--generate",
        action="store_true",
        help="make a new key, store it, and print it once (this is the only time it is shown)",
    )
    backup_key.add_argument(
        "--set", dest="set_key", help="store a key you already have (base64, 32 bytes)"
    )
    agent = commands.add_parser(
        "restore-agent",
        help="run the helper that replaces the volumes a running stack cannot replace",
    )
    agent.add_argument("--host", default="0.0.0.0")  # noqa: S104 - compose network only
    agent.add_argument("--port", type=int, default=8100)
    commands.add_parser("backup-preflight", help="check that the next backup would work")
    backup_list = commands.add_parser("backup-list", help="list backups at the destination")
    backup_list.add_argument("--limit", type=int, default=50)
    backup_verify = commands.add_parser(
        "backup-verify", help="read a backup back and re-check every checksum"
    )
    backup_verify.add_argument("backup_id")
    backup_verify.add_argument(
        "--shallow",
        action="store_true",
        help="check stored checksums only, without decrypting (faster, proves less)",
    )
    backup_prune = commands.add_parser("backup-prune", help="apply the retention rules")
    backup_prune.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it the command only reports what it would delete",
    )
    backup_restore = commands.add_parser(
        "backup-restore", help="stage a backup locally, and optionally apply it"
    )
    backup_restore.add_argument("backup_id")
    backup_restore.add_argument(
        "--stage-to", required=True, help="directory to download, decrypt and verify into"
    )
    backup_restore.add_argument(
        "--component", action="append", default=[], help="restrict to these components"
    )
    backup_restore.add_argument(
        "--reuse-staged",
        action="store_true",
        help=(
            "keep components already in --stage-to that still match the manifest, instead "
            "of transferring and decrypting them again"
        ),
    )
    backup_restore.add_argument(
        "--apply-databases", action="store_true", help="pg_restore the staged dumps (destructive)"
    )
    backup_restore.add_argument(
        "--apply-search-index", action="store_true", help="restore the OpenSearch snapshot"
    )
    backup_restore.add_argument(
        "--apply-files", action="store_true", help="extract staged archives over their directories"
    )
    backup_restore.add_argument(
        "--i-understand-this-destroys-current-data",
        action="store_true",
        help="required by every --apply-* flag",
    )

    run = commands.add_parser("run", help="process pending pipeline stages")
    run.add_argument("--limit", type=int)
    run.add_argument("--enable-evals", action="store_true")
    worker = commands.add_parser("hatchet-worker", help="run the Hatchet insertion worker")
    worker.add_argument(
        "--slots",
        type=int,
        default=int(os.environ.get("KI_HATCHET_WORKER_SLOTS", "32")),
    )
    commands.add_parser(
        "watch", help="continuously monitor local sources and index changes as they happen"
    )
    commands.add_parser("status", help="print pipeline state counts")
    rotate = commands.add_parser(
        "rotate-connector-key",
        help="re-encrypt stored connector credentials under a new key",
    )
    rotate.add_argument(
        "--new-key",
        required=True,
        help="the replacement base64 32-byte key (generate with `openssl rand -base64 32`)",
    )
    rotate.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be re-encrypted without writing",
    )
    benchmark = commands.add_parser(
        "generate-benchmark",
        help="pack a legal-task-set checkout into a law-firm corpus (gold is opt-in via --gold)",
    )
    benchmark.add_argument("output")
    benchmark.add_argument("--source", required=True, help="path to the task-set checkout (see the Benchmarks page in the docs)")
    benchmark.add_argument(
        "--areas", default="", help="comma-separated task areas, e.g. contracts/banking"
    )
    benchmark.add_argument("--matters", type=int, default=50)
    benchmark.add_argument("--docs-target", type=int, default=1000)
    benchmark.add_argument("--seed", type=int, default=42)
    benchmark.add_argument(
        "--layout",
        default="flat",
        choices=["flat", "firm"],
        help="flat (matter=instrument) or firm (realistic Client/Matter/Workstream)",
    )
    benchmark.add_argument(
        "--firm-naming",
        default="deterministic",
        choices=["deterministic", "llm"],
        help="firm layout: client/counterparty from filenames (free) or an LLM "
        "(canonical legal names; needs the gateway)",
    )
    benchmark.add_argument(
        "--firm-naming-model",
        default="",
        help="firm-naming llm: gateway model (default: KI_LLM_MODEL)",
    )
    benchmark.add_argument(
        "--structure",
        help="firm layout: a curated structure manifest (JSON) mapping scenarios to "
        "client/matter/counterparty. Applied verbatim; supersedes --firm-naming. "
        "Use the committed firm structure via its name (e.g. contracts-banking).",
    )
    benchmark.add_argument(
        "--noise",
        default="none",
        choices=["none", "light", "heavy"],
        help="firm layout: inject realistic DMS mess (flat matters, renamed folders, "
        "junk). Deterministic and gold-safe. Default: none (clean).",
    )
    benchmark.add_argument(
        "--gold",
        default="",
        help="comma-separated gold kinds to generate: working_set, factoid, qa "
        "(default: none — the corpus only)",
    )
    benchmark.add_argument("--per-scenario", type=int, default=4, help="qa: questions per scenario")
    benchmark.add_argument(
        "--gold-model",
        default="",
        help="qa: gateway model for question generation (default: KI_LLM_MODEL)",
    )

    llm_gold = commands.add_parser(
        "derive-llm-gold",
        help="append LLM-generated, source-verified retrieval gold (needs the gateway)",
    )
    llm_gold.add_argument("corpus_dir", help="a generate-benchmark output directory")
    llm_gold.add_argument("--per-scenario", type=int, default=4)
    llm_gold.add_argument(
        "--model", default="", help="gateway model (default: KI_LLM_MODEL)"
    )
    llm_gold.add_argument("--limit-scenarios", type=int, help="cap scenarios (for a cheap dry run)")

    freeze = commands.add_parser(
        "freeze-gold", help="commit a reviewed gold set into the benchmark package (one-time)"
    )
    freeze.add_argument("corpus_dir", help="a generate-benchmark output directory")
    freeze.add_argument("--name", required=True, help="frozen set name, e.g. contracts-banking")

    retrieval_eval = commands.add_parser(
        "run-retrieval-eval", help="score a frozen gold set against the live index"
    )
    retrieval_eval.add_argument("gold", help="a frozen set name or a path to a *.gold.jsonl file")
    retrieval_eval.add_argument(
        "--baseline",
        default="ladder",
        help="one baseline (bm25|naive_dense|full) or 'ladder' for the gated comparison",
    )
    retrieval_eval.add_argument("--report", help="write the full report JSON here")
    retrieval_eval.add_argument("--min-lift", type=float, default=0.05)

    qa_eval = commands.add_parser(
        "run-qa-eval",
        help="ask an agent QA questions (llm_question gold) and score answers + recall",
    )
    qa_eval.add_argument("gold", help="a retrieval-gold.jsonl (uses its llm_question labels)")
    qa_eval.add_argument("--limit", type=int, help="cap number of questions")
    qa_eval.add_argument("--agent-model", default="", help="gateway model (default: KI_LLM_MODEL)")
    qa_eval.add_argument("--judge-model", default="", help="gateway model (default: KI_LLM_MODEL)")
    qa_eval.add_argument("--report", help="write the full report JSON here")

    bench_ingest = commands.add_parser(
        "bench-ingest",
        help="add-source + sync + run a corpus, measuring throughput and OpenAI cost",
    )
    bench_ingest.add_argument("corpus_dir", help="a generate-benchmark output directory")
    bench_ingest.add_argument("--name", default="benchmark-corpus")
    bench_ingest.add_argument("--report", help="write the measurement JSON here")

    task_eval = commands.add_parser(
        "run-task-eval",
        help="real-usage benchmark: agent/RAG produce work product, scored by rubric",
    )
    task_eval.add_argument("corpus_dir", help="a generate-benchmark output directory")
    task_eval.add_argument(
        "--modes",
        default="closed_book,classic_rag,agentic,oracle",
        help="comma-separated baseline ladder",
    )
    task_eval.add_argument("--limit-tasks", type=int, help="cap number of tasks (cost control)")
    task_eval.add_argument("--max-steps", type=int, default=25, help="agent tool-loop ceiling")
    task_eval.add_argument(
        "--agent-model", default="", help="gateway model for the agent (default: KI_LLM_MODEL)"
    )
    task_eval.add_argument(
        "--judge-model", default="", help="gateway model for the judge (default: KI_LLM_MODEL)"
    )
    task_eval.add_argument("--report", help="write the full report JSON here")

    serve = commands.add_parser("serve", help="run admin UI, API, and MCP endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "restore-agent":
        # Before any database setup: this process exists to stop and start two containers
        # and unpack two archives. It never reads the appliance's data, and a helper that
        # cannot start because Postgres is down is a helper that is missing during exactly
        # the recovery it was built for.
        import uvicorn

        from knowledge_index.backup.volume_agent import create_agent

        uvicorn.run(create_agent(), host=args.host, port=args.port, log_level="info")
        return
    if args.command == "generate-benchmark":
        # packs the corpus (no database); gold is opt-in via --gold
        from knowledge_index.benchmark import build_task_corpus

        if (args.firm_naming == "llm" or args.structure or args.noise != "none") and (
            args.layout != "firm"
        ):
            raise SystemExit("--firm-naming llm / --structure / --noise require --layout firm")
        structure = _resolve_structure(args.structure) if args.structure else None
        resolve_parties = None
        if args.layout == "firm" and args.firm_naming == "llm" and not structure:
            # LLM client/counterparty resolution — needs the model gateway
            from knowledge_index.benchmark.firm_parties_llm import make_llm_party_resolver

            config = ConfigStore(args.config).get()
            resolve_parties = make_llm_party_resolver(
                config, model=_model_flag(args.firm_naming_model)
            )
        summary = build_task_corpus(
            args.output,
            args.source,
            areas=[a for a in args.areas.split(",") if a.strip()],
            matters=args.matters,
            docs_target=args.docs_target,
            seed=args.seed,
            layout=args.layout,
            resolve_parties=resolve_parties,
            structure=structure,
            noise_level=args.noise,
        )
        requested = [k.strip() for k in args.gold.split(",") if k.strip()]
        unknown = [k for k in requested if k not in ("working_set", "factoid", "qa")]
        if unknown:
            raise SystemExit(f"unknown --gold kind(s): {unknown}; use working_set, factoid, qa")
        gold: dict = {}
        deterministic = tuple(
            "instruction_working_set" if k == "working_set" else k
            for k in requested
            if k in ("working_set", "factoid")
        )
        if deterministic:
            from knowledge_index.benchmark import write_gold

            gold["deterministic"] = write_gold(args.output, kinds=deterministic)
        if "qa" in requested:
            # LLM question gold — needs the model gateway (reachable litellm URL)
            from knowledge_index.benchmark import generate_llm_gold

            config = ConfigStore(args.config).get()
            gold["qa"] = generate_llm_gold(
                args.output,
                config,
                per_scenario=args.per_scenario,
                model=_model_flag(args.gold_model),
            )
        summary["gold"] = gold or "none — pass --gold working_set,factoid,qa"
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    if args.command == "derive-llm-gold":
        # needs the model gateway + config, but no database or index
        from knowledge_index.benchmark import generate_llm_gold

        config = ConfigStore(args.config).get()
        stats = generate_llm_gold(
            args.corpus_dir,
            config,
            per_scenario=args.per_scenario,
            model=_model_flag(args.model),
            limit_scenarios=args.limit_scenarios,
        )
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return
    if args.command == "freeze-gold":
        # pure file copy — commit gold into the benchmark package, no database
        from knowledge_index.benchmark import freeze

        print(json.dumps(freeze(args.corpus_dir, args.name), indent=2, ensure_ascii=False))
        return
    init_db()
    factory = sessionmaker(get_engine(), expire_on_commit=False)
    store = ConfigStore(args.config)
    # The CLI writes to the same index the API serves, so it must apply the same access
    # rules — a sync run that mirrors ACLs under different semantics than the reader is
    # how ethical walls end up inconsistent.
    _security = store.get().security
    configure_access(
        source_acl_mode=_security.source_acl_mode,
        principal_aliases=_security.principal_aliases,
    )

    if args.command == "init":
        store.get().artifact_dir.mkdir(parents=True, exist_ok=True)
        print("Knowledge Index initialized")
    elif args.command == "add-source":
        source_root = Path(args.root).expanduser().resolve(strict=True)
        if not source_root.is_dir():
            raise SystemExit("source root must be a directory")
        with factory() as session:
            source_config: dict = {"root": str(source_root)}
            if args.acl_map:
                source_config["acl_by_path"] = json.loads(
                    Path(args.acl_map).read_text(encoding="utf-8")
                )
            else:
                source_config["default_acl"] = [
                    {
                        "principal": args.principal,
                        "principal_kind": "group",
                        "access": "allow",
                    }
                ]
            source = Source(
                kind="local_fs",
                display_name=args.name or source_root.name,
                config=source_config,
            )
            session.add(source)
            session.commit()
            print(source.id)
    elif args.command == "sync":
        # Same enqueue path as the UI button and the watcher, so a CLI sync is a run in
        # the ledger like any other. The command then blocks on those runs because an
        # operator typing it is waiting for an answer, not for a receipt.
        from knowledge_index.sync.runs import enqueue_sync, wait_for_run

        enqueued = enqueue_sync(factory, store.get(), trigger="cli")
        for skipped in enqueued.skipped:
            print(f"skipped {skipped.source_id}: {skipped.reason}", file=sys.stderr)
        results = [wait_for_run(factory, run.run_id) for run in enqueued.runs]
        print(json.dumps(results, indent=2))
        if any(row["status"] == "failed" for row in results):
            raise SystemExit(1)
    elif args.command == "backup":
        # Same enqueue path as the UI button and the schedule, so a CLI backup is a run in
        # the ledger like any other, and blocks by default because an operator typing it
        # is waiting for an answer.
        from knowledge_index.backup.runs import (
            BackupNotConfigured,
            enqueue_backup,
            wait_for_run,
        )

        try:
            enqueued = enqueue_backup(factory, store.get(), trigger="cli", force=args.force)
        except BackupNotConfigured as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        if args.no_wait:
            print(json.dumps(enqueued.payload(), indent=2))
        else:
            result = wait_for_run(factory, enqueued.run_id)
            print(json.dumps(result, indent=2, default=str))
            if result["status"] == "failed":
                raise SystemExit(1)
    elif args.command == "backup-key":
        # The headless equivalent of the Generate button. Backups are configured from the
        # admin UI, but an appliance built by a deployment script has no browser, and the
        # alternative to this is telling people to run base64 by hand.
        from knowledge_index.backup import secrets as backup_secrets

        if args.generate and args.set_key:
            print("Use either --generate or --set, not both.", file=sys.stderr)
            raise SystemExit(2)
        if args.generate or args.set_key:
            value = args.set_key or backup_secrets.generate_key()
            try:
                status = backup_secrets.store(
                    backup_secrets.ENCRYPTION_KEY, value, factory
                )
            except backup_secrets.BackupSecretError as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2) from exc
            if args.generate:
                print(value)
                print(
                    "\nThat is the only time this key is printed. Save it somewhere that is "
                    "not this appliance — without it the backups cannot be opened by anyone.",
                    file=sys.stderr,
                )
            print(json.dumps(status.payload(), indent=2), file=sys.stderr)
        else:
            status = backup_secrets.status(backup_secrets.ENCRYPTION_KEY, factory)
            print(json.dumps(status.payload(), indent=2))
            if not status.set:
                raise SystemExit(1)
    elif args.command == "backup-preflight":
        from knowledge_index.backup.runs import preflight

        report = preflight(store.get(), factory)
        print(json.dumps(report, indent=2, default=str))
        if not report["ok"]:
            raise SystemExit(1)
    elif args.command == "backup-list":
        from knowledge_index.backup.runs import list_backups

        print(json.dumps(list_backups(store.get(), limit=args.limit), indent=2, default=str))
    elif args.command == "backup-verify":
        from knowledge_index.backup.runs import verify_backup

        report = verify_backup(store.get(), args.backup_id, deep=not args.shallow)
        print(json.dumps(report, indent=2, default=str))
        if not report["ok"]:
            raise SystemExit(1)
    elif args.command == "backup-prune":
        from knowledge_index.backup.runs import prune_backups

        print(json.dumps(prune_backups(store.get(), dry_run=not args.apply), indent=2, default=str))
    elif args.command == "backup-restore":
        raise SystemExit(_backup_restore(store.get(), args, factory))
    elif args.command == "run":
        config = store.get().model_copy(deep=True)
        if args.enable_evals:
            config.pipeline.stages["gen_evals"].enabled = True
        result = PipelineRunner(factory, config).run_until_idle(limit=args.limit)
        print(json.dumps(result.__dict__, indent=2))
    elif args.command == "hatchet-worker":
        from knowledge_index.orchestration import start_hatchet_worker

        # pass the getter, not a snapshot: admin config changes (model assignments,
        # stage toggles) must reach running workers without a restart
        start_hatchet_worker(factory, store.get, slots=args.slots)
    elif args.command == "watch":
        from knowledge_index.sync.watch import run_watch_loop

        # pass the getter so orchestrator/model config changes reach the watcher live
        run_watch_loop(factory, store.get)
    elif args.command == "rotate-connector-key":
        raise SystemExit(_rotate_connector_key(factory, args.new_key, dry_run=args.dry_run))
    elif args.command == "status":
        from collections import Counter

        from knowledge_index.taxonomies import stage_bucket

        with factory() as session:
            rows = session.execute(
                select(ProcessingState.stage, ProcessingState.status, ProcessingState.last_error)
            ).all()
        # Same bucketing as /api/status: a stage waiting on its predecessor is not a
        # stage the pipeline skipped.
        counts = Counter((stage, stage_bucket(status, error)) for stage, status, error in rows)
        print(
            json.dumps(
                [
                    {"stage": stage, "status": bucket, "count": count}
                    for (stage, bucket), count in sorted(counts.items())
                ],
                indent=2,
            )
        )
    elif args.command == "run-retrieval-eval":
        from knowledge_index.benchmark import evaluate, evaluate_ladder, resolve
        from knowledge_index.benchmark.harness import CorpusCoverageError

        gold_file = resolve(args.gold)
        try:
            if args.baseline == "ladder":
                report = evaluate_ladder(factory, store.get(), gold_file, min_lift=args.min_lift)
                passed = report["gate"]["passed"]
            else:
                report = evaluate(factory, store.get(), gold_file, baseline=args.baseline)
                passed = report["ethical_wall"]["clean"]
        except CorpusCoverageError as exc:
            failure = {"error": str(exc), "corpus": exc.coverage}
            print(json.dumps(failure, indent=2, ensure_ascii=False))
            raise SystemExit(1) from exc
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if args.report:
            Path(args.report).write_text(rendered, encoding="utf-8")
        print(rendered)
        if not passed:
            raise SystemExit(1)
    elif args.command == "bench-ingest":
        import time

        from knowledge_index.benchmark.measure import read_litellm_spend, spend_delta
        from knowledge_index.db.models import SourceObject

        config = store.get()
        corpus = Path(args.corpus_dir)
        source_root = (corpus / "mock_dms").resolve(strict=True)
        acl_map = json.loads((corpus / "acl-by-path.json").read_text(encoding="utf-8"))
        with factory() as session:
            source = Source(
                kind="local_fs",
                display_name=args.name,
                config={"root": str(source_root), "acl_by_path": acl_map},
            )
            session.add(source)
            session.flush()
            source_id = source.id
            sync_start = time.monotonic()
            SyncEngine(
                    session,
                    source,
                    connector_from_source(source, session),
                    selection_fingerprint=_selection_fingerprint(source),
                    acl_refresh_hours=store.get().security.acl_refresh_hours,
                ).sync()
            sync_seconds = time.monotonic() - sync_start
            session.commit()
        with factory() as session:
            docs = (
                session.scalar(
                    select(func.count())
                    .select_from(SourceObject)
                    .where(SourceObject.source_id == source_id)
                )
                or 0
            )
        before = read_litellm_spend(config)
        run_start = time.monotonic()
        result = PipelineRunner(factory, config).run_until_idle()
        run_seconds = time.monotonic() - run_start
        cost = spend_delta(before, read_litellm_spend(config))
        report = {
            "corpus_dir": str(corpus),
            "documents": docs,
            "throughput": {
                "sync_seconds": round(sync_seconds, 2),
                "run_seconds": round(run_seconds, 2),
                "docs_per_hour": round(docs / run_seconds * 3600, 1) if run_seconds else None,
            },
            "pipeline": result.__dict__,
            "cost_usd": cost,
            "cost_per_1000_docs": (
                round(cost["total"] / docs * 1000, 4)
                if cost.get("total") is not None and docs
                else None
            ),
        }
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if args.report:
            Path(args.report).write_text(rendered, encoding="utf-8")
        print(rendered)
    elif args.command == "run-qa-eval":
        from knowledge_index.benchmark.qa_eval import evaluate_qa

        config = store.get()
        report = evaluate_qa(
            factory,
            config,
            args.gold,
            agent_model=_model_flag(args.agent_model),
            judge_model=_model_flag(args.judge_model),
            limit=args.limit,
        )
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if args.report:
            Path(args.report).write_text(rendered, encoding="utf-8")
        print(rendered)
    elif args.command == "run-task-eval":
        from knowledge_index.benchmark.task_eval import evaluate_tasks

        config = store.get()
        report = evaluate_tasks(
            factory,
            config,
            args.corpus_dir,
            modes=tuple(m for m in args.modes.split(",") if m.strip()),
            agent_model=_model_flag(args.agent_model),
            judge_model=_model_flag(args.judge_model),
            limit=args.limit_tasks,
            max_steps=args.max_steps,
        )
        rendered = json.dumps(report, indent=2, ensure_ascii=False)
        if args.report:
            Path(args.report).write_text(rendered, encoding="utf-8")
        print(rendered)
    elif args.command == "serve":
        import uvicorn

        from knowledge_index.backup.scheduler import (
            start_background_scheduler as start_backup_scheduler,
        )
        from knowledge_index.connectors.events import start_background_event_manager
        from knowledge_index.sync.scheduler import start_background_scheduler
        from knowledge_index.web.app import create_app

        # Every continuous source gets synced on its interval from here, whatever kind it
        # is. Started by the command rather than by create_app so that importing the app
        # — in a test, in a script — never starts crawling a firm's estate. The app is the
        # process that exists in every deployment: the Hatchet worker does not exist under
        # the in-process orchestrator, and a firm with no mounted folders has no reason to
        # run the watcher. The config getter is passed, not a snapshot, so an admin
        # changing the orchestrator or the confirmation threshold reaches it live.
        start_background_scheduler(factory, store.get)
        # Provider notifications are outbound-pull transports (Pub/Sub and Event Hubs),
        # so an on-prem appliance needs no public webhook. They wake the same delta sync
        # as the button and scheduler; the interval stays as reconciliation for missed
        # events. The manager also renews expiring provider subscriptions.
        start_background_event_manager(factory, store.get)
        # And the nightly backup, from the same process and for the same reason: the app
        # is the one container every deployment runs. Whether a backup is actually taken
        # is governed by backup.schedule.enabled in the admin UI; this only decides that
        # something is watching the clock.
        start_backup_scheduler(factory, store.get)
        uvicorn.run(create_app(factory, store), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()


def _backup_restore(config, args, factory) -> int:
    """Stage a backup, then apply only the parts the operator explicitly asked for.

    Staging always happens and is always safe: it downloads, decrypts and re-checks every
    component against the manifest, and writes nothing outside ``--stage-to``. Practising
    a restore this way, on a live appliance, is the point — "zero unverified restores" is
    a property a firm has to establish before the day it needs it.

    Applying is the destructive half, so each store is a separate flag, all of them
    require the long confirmation flag, and anything the plan lists as a blocker stops the
    whole thing. The two volume components are not applyable from here at all: replacing
    Keycloak's data volume means stopping Keycloak, which a process inside the stack
    cannot do to itself.
    """
    from knowledge_index.backup import restore as backup_restore
    from knowledge_index.backup.runs import BackupNotFound

    applying = args.apply_databases or args.apply_search_index or args.apply_files
    if applying and not args.i_understand_this_destroys_current_data:
        print(
            "--apply-* replaces live data with the contents of the backup and cannot be "
            "undone. Re-run with --i-understand-this-destroys-current-data once the "
            "appliance is stopped and you mean it.",
            file=sys.stderr,
        )
        return 2

    try:
        plan = backup_restore.restore_plan(config, args.backup_id, factory)
    except BackupNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for warning in plan["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    if plan["blockers"]:
        for blocker in plan["blockers"]:
            print(f"blocked: {blocker}", file=sys.stderr)
        return 1

    staged = backup_restore.stage_backup(
        config,
        args.backup_id,
        Path(args.stage_to),
        only=args.component or None,
        reuse=args.reuse_staged,
        session_factory=factory,
    )
    outcome: dict = {
        "backup_id": args.backup_id,
        "staged_to": args.stage_to,
        "staged": [
            {"name": item.name, "kind": item.kind, "bytes": item.bytes, "path": str(item.path)}
            for item in staged
        ],
        "applied": [],
    }
    for item in staged:
        try:
            if item.kind == "postgres" and args.apply_databases:
                outcome["applied"].append(backup_restore.apply_database(config, item))
            elif item.kind == "opensearch" and args.apply_search_index:
                outcome["applied"].append(backup_restore.apply_search_index(config, item))
            elif item.kind == "files" and args.apply_files:
                outcome["applied"].append(backup_restore.apply_files(config, item))
        except backup_restore.RestoreError as exc:
            print(f"{item.name}: {exc}", file=sys.stderr)
            print(json.dumps(outcome, indent=2, default=str))
            return 1
    print(json.dumps(outcome, indent=2, default=str))

    # A restore that half worked must not exit 0. pg_restore is not run with
    # --exit-on-error, deliberately, so a store can come back with errors and still be
    # reported here; anything that reported serious ones is a failed restore, and
    # scripts/restore-backup.sh runs under `set -e` and would otherwise print "Restore
    # complete" over the top of it.
    failed = [item for item in outcome["applied"] if item.get("ok") is False]
    if failed:
        print("", file=sys.stderr)
        for item in failed:
            print(f"{item['component']}: pg_restore reported errors:", file=sys.stderr)
            for line in item.get("serious_errors", []):
                print(f"  {line}", file=sys.stderr)
        print(
            "\nThis restore is incomplete. The appliance now holds part of the backup and "
            "part of whatever was there before; do not start it and call it recovered.",
            file=sys.stderr,
        )
        return 1

    if not applying:
        print(
            f"\nStaged and verified into {args.stage_to}. Nothing was applied — pass the "
            "--apply-* flags, or use scripts/restore-backup.sh for a whole-stack restore "
            "including the Keycloak and Hatchet volumes.",
            file=sys.stderr,
        )
    return 0


def _rotate_connector_key(factory, new_key: str, *, dry_run: bool) -> int:
    """Re-encrypt every stored connector credential under a new key.

    Read with the key currently in the environment, written back under the new one, in a
    single transaction: a partial rotation would leave some connections readable and
    others not, with no way to tell which without trying each.

    The operator swaps KI_CONNECTOR_CREDENTIAL_KEY to the new value *after* this reports
    success, so the old key must still be in the environment while it runs.
    """
    from sqlalchemy import select

    from knowledge_index.connectors.runtime.secrets import (
        CredentialCryptoError,
        decrypt_credentials,
        encrypt_credentials,
        key_fingerprint,
    )
    from knowledge_index.db.models import SourceCredential

    try:
        old_fingerprint = key_fingerprint()
        new_fingerprint = key_fingerprint(key=new_key)
    except CredentialCryptoError as exc:
        print(f"cannot rotate: {exc}")
        return 2
    if old_fingerprint == new_fingerprint:
        print("the new key is identical to the current one; nothing to do")
        return 1

    with factory() as session:
        rows = list(session.scalars(select(SourceCredential)))
        if not rows:
            print("no stored connector credentials; nothing to rotate")
            return 0

        rotated, skipped = [], []
        for row in rows:
            if row.key_fingerprint and row.key_fingerprint == new_fingerprint:
                # Already migrated by an interrupted earlier run.
                skipped.append((row.source_id, "already under the new key"))
                continue
            try:
                plaintext = decrypt_credentials(row.payload)
            except CredentialCryptoError as exc:
                skipped.append((row.source_id, str(exc).split(".")[0]))
                continue
            rotated.append((row, plaintext))

        for source_id, reason in skipped:
            print(f"skipped {source_id}: {reason}")
        if not rotated:
            print("nothing could be re-encrypted")
            return 1 if skipped else 0

        if dry_run:
            print(f"would re-encrypt {len(rotated)} credential(s) "
                  f"from key {old_fingerprint} to {new_fingerprint}")
            return 0

        for row, plaintext in rotated:
            row.payload = encrypt_credentials(plaintext, key=new_key)
            row.key_fingerprint = new_fingerprint
        session.commit()

    print(f"re-encrypted {len(rotated)} credential(s) from key "
          f"{old_fingerprint} to {new_fingerprint}")
    print("now set KI_CONNECTOR_CREDENTIAL_KEY to the new key on every container "
          "(app, worker, watcher) and restart them.")
    if skipped:
        print(f"{len(skipped)} row(s) were not rotated — re-authorize those connections.")
    return 0


def _selection_fingerprint(source) -> str:
    """Digest of the folder selection this sync is running under."""
    from knowledge_index.connectors import scoping

    return scoping.fingerprint((source.config or {}).get("connector"))
