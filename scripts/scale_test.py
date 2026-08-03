#!/usr/bin/env python3
"""Scale-test harness: generate a parameterized corpus, ingest it through the real
stack, and measure sync rate, per-stage throughput/latency, query latency under
concurrent ACL-scoped load, spend, and footprint. See docs/scale-testing.md.

No synthetic-latency mode: this script requires the live compose stack and calls
real models. Unavailable measurements are reported as unavailable, never estimated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowledge_index.fixtures import _contract_docx, _email, _grant, _simple_docx  # noqa: E402

ADMIN_HEADERS = {"x-ki-principals": "user:scale-admin,role:admin"}
STAGES = [
    "fetch",
    "convert",
    "classify_matter",
    "relate",
    "extract_metadata",
    "extract_decisions",
    "gen_evals",
    "index",
]


def generate_corpus(out: Path, *, docs: int, matters: int) -> dict:
    """Deterministic corpus: contract chains, annexes, mail threads, poison files."""
    root = out / "mock_dms"
    if root.exists():
        raise SystemExit(f"corpus dir already exists: {root} — remove it or pick --out")
    root.mkdir(parents=True)
    acl_by_path: dict[str, list[dict]] = {}
    groups = ["group:ma-team", "group:litigation", "group:real-estate", "group:labor"]
    produced = 0
    matter_index = 0
    while produced < docs:
        matter_index += 1
        ref = f"M-2026-{matter_index:04d}"
        group = groups[matter_index % len(groups)]
        folder = root / "Mandate" / f"{ref} Scale-Mandat {matter_index}"
        folder.mkdir(parents=True)
        acl = [_grant(group)]

        def emit(path: Path) -> None:
            nonlocal produced
            acl_by_path[path.relative_to(root).as_posix()] = acl
            produced += 1

        draft = folder / "Vertrag_Entwurf_v1.docx"
        _contract_docx(
            draft,
            title=f"Entwurf Dienstleistungsvertrag — Mandat {matter_index}",
            liability="Die Haftung der Auftragnehmerin ist unbeschränkt.",
        )
        emit(draft)
        if produced >= docs:
            break
        final = folder / "Vertrag_final.docx"
        _contract_docx(
            final,
            title=f"Dienstleistungsvertrag — Mandat {matter_index}",
            liability="Die Haftung der Auftragnehmerin ist auf die Vergütung begrenzt.",
        )
        emit(final)
        if produced >= docs:
            break
        annex = folder / "Anlage_1_Leistungsbeschreibung.txt"
        annex.write_text(
            f"{ref}\nAnlage 1 zum Dienstleistungsvertrag\nLeistungsbeschreibung.\n",
            encoding="utf-8",
        )
        emit(annex)
        if produced >= docs:
            break
        mail = folder / "Korrespondenz_Haftung.eml"
        _email(
            mail,
            subject=f"{ref} – Haftungsbegrenzung",
            message_id=f"<scale-{matter_index}@mock.kanzlei>",
            body=(
                "Die unbeschränkte Haftung ist aus Risikosicht nicht vertretbar; "
                "bitte auf die Vergütung begrenzen."
            ),
        )
        emit(mail)
        if produced >= docs:
            break
        note = folder / "Vermerk_final.txt"
        _simple_docx  # not used for txt; keep note simple
        note.write_text(
            f"{ref}\nVermerk zur Vertragsverhandlung: Haftung auf Vergütung begrenzt.",
            encoding="utf-8",
        )
        emit(note)
        if matter_index % 10 == 0 and produced < docs:
            poison = folder / "scan_alt.bin"
            poison.write_bytes(b"\x00\xff-not-a-document-\x00")
            emit(poison)
        if matter_index >= matters and produced < docs:
            # distribute remaining docs across existing matters round-robin
            matters += 1
    (out / "acl-by-path.json").write_text(json.dumps(acl_by_path, indent=1), encoding="utf-8")
    return {"root": str(root), "files": produced, "matters": matter_index}


def wait_for_runs(client: httpx.Client, run_ids: list[str], *, timeout_s: int) -> list[dict]:
    """Poll named pipeline runs to a terminal state."""
    if not run_ids:
        return []
    wanted = set(run_ids)
    deadline = time.monotonic() + timeout_s
    while True:
        runs = [row for row in client.get("/api/runs").json() if row["id"] in wanted]
        if len(runs) == len(wanted) and all(
            row["status"] in ("completed", "failed") for row in runs
        ):
            return runs
        if time.monotonic() > deadline:
            raise SystemExit(f"runs did not finish in {timeout_s}s: {run_ids}")
        time.sleep(5)


def wait_settled(app: str, *, timeout_s: int) -> dict:
    deadline = time.monotonic() + timeout_s
    with httpx.Client(base_url=app, headers=ADMIN_HEADERS, timeout=30) as client:
        while True:
            status = client.get("/api/status").json()
            pipeline = status.get("pipeline", {})
            open_count = sum(
                counts.get(state, 0)
                for counts in pipeline.values()
                for state in ("pending", "running", "failed")
            )
            runs = client.get("/api/runs").json()
            active = [r for r in runs if r["status"] in ("queued", "running")]
            if open_count == 0 and not active:
                return status
            if time.monotonic() > deadline:
                raise SystemExit(f"pipeline did not settle in {timeout_s}s; open={open_count}")
            time.sleep(10)


def stage_metrics(db_url: str) -> list[dict]:
    engine = create_engine(db_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT stage, status, count(*), "
                "extract(epoch FROM max(updated_at) - min(updated_at)) AS span_s "
                "FROM processing_state GROUP BY stage, status"
            )
        ).all()
    by_stage: dict[str, dict] = {stage: {"stage": stage} for stage in STAGES}
    for stage, status, count, span in rows:
        entry = by_stage.setdefault(stage, {"stage": stage})
        entry[status] = count
        if status == "done" and span and span > 0:
            entry["done_per_hour"] = round(count / (span / 3600), 1)
    return [by_stage[stage] for stage in STAGES if stage in by_stage]


async def query_load(app: str, *, users: int, per_user: int) -> dict:
    queries = [
        ("group:ma-team", {"query": "Haftung Vergütung begrenzt", "limit": 10}),
        ("group:litigation", {"query": "Anlage Leistungsbeschreibung", "limit": 10}),
        ("group:real-estate", {"query": "Dienstleistungsvertrag Haftung", "limit": 10}),
        ("user:outsider", {"query": "Haftung", "limit": 10}),
    ]
    latencies: list[float] = []
    outsider_hits = 0
    errors = 0

    async def one_user(index: int) -> None:
        nonlocal outsider_hits, errors
        async with httpx.AsyncClient(base_url=app, timeout=60) as client:
            for turn in range(per_user):
                principal, body = queries[(index + turn) % len(queries)]
                started = time.perf_counter()
                response = await client.post(
                    "/api/search", json=body, headers={"x-ki-principals": principal}
                )
                elapsed = time.perf_counter() - started
                if response.status_code != 200:
                    errors += 1
                    continue
                latencies.append(elapsed)
                if principal == "user:outsider" and response.json().get("hits"):
                    outsider_hits += len(response.json()["hits"])

    await asyncio.gather(*(one_user(i) for i in range(users)))
    latencies.sort()

    def pct(p: float) -> float | None:
        return round(statistics.quantiles(latencies, n=100)[int(p) - 1] * 1000, 1) if len(
            latencies
        ) >= 100 else (round(latencies[int(len(latencies) * p / 100)] * 1000, 1) if latencies else None)

    return {
        "requests": len(latencies),
        "errors": errors,
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
        "outsider_hits_MUST_BE_ZERO": outsider_hits,
    }


def spend(db_url: str) -> dict:
    """Read real LiteLLM spend logs; report unavailability loudly, never estimate."""
    litellm_url = db_url.rsplit("/", 1)[0] + "/litellm"
    try:
        engine = create_engine(litellm_url)
        with engine.connect() as connection:
            row = connection.execute(
                text('SELECT count(*), coalesce(sum(spend),0) FROM "LiteLLM_SpendLogs"')
            ).one()
        return {"requests": int(row[0]), "usd": float(round(row[1], 4))}
    except Exception as exc:
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def footprint(db_url: str) -> dict:
    engine = create_engine(db_url)
    with engine.connect() as connection:
        pg_size = connection.execute(
            text("SELECT pg_size_pretty(pg_database_size(current_database()))")
        ).scalar()
    try:
        opensearch = httpx.get(
            "http://127.0.0.1:9200/knowledge-index-chunks-v1/_stats/store", timeout=10
        ).json()
        index_bytes = opensearch["_all"]["total"]["store"]["size_in_bytes"]
    except Exception as exc:
        index_bytes = f"unavailable: {type(exc).__name__}"
    return {"postgres": pg_size, "opensearch_index_bytes": index_bytes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=100)
    parser.add_argument("--matters", type=int, default=10)
    parser.add_argument("--out", type=Path, default=Path("testdata/scale"))
    parser.add_argument("--app", default="http://127.0.0.1:8000")
    parser.add_argument("--db", default="postgresql+pg8000://ki:ki-dev-only@localhost:5439/ki")
    parser.add_argument("--container-root", default="/testdata/scale/mock_dms",
                        help="corpus path as seen inside the app container")
    parser.add_argument("--query-users", type=int, default=8)
    parser.add_argument("--queries-per-user", type=int, default=10)
    parser.add_argument("--settle-timeout", type=int, default=14400)
    parser.add_argument("--report", type=Path, default=Path("scale-report.json"))
    args = parser.parse_args()

    report: dict = {"parameters": vars(args) | {"out": str(args.out), "report": str(args.report)}}

    print(f"[1/5] generating corpus: {args.docs} docs / ~{args.matters} matters")
    report["corpus"] = generate_corpus(args.out, docs=args.docs, matters=args.matters)

    with httpx.Client(base_url=args.app, headers=ADMIN_HEADERS, timeout=120) as client:
        print("[2/5] registering source + sync")
        acl_map = json.loads((args.out / "acl-by-path.json").read_text(encoding="utf-8"))
        source = client.post(
            "/api/sources",
            json={
                "display_name": f"Scale corpus ({args.docs})",
                "kind": "local_fs",
                "root": args.container_root,
                "acl_by_path": acl_map,
                "sync_policy": {"mode": "manual"},
            },
        )
        source.raise_for_status()
        started = time.perf_counter()
        # Sync is orchestrated: the POST reserves runs and returns. Timing the request
        # would measure the enqueue, so wait for the runs it created.
        enqueued = client.post("/api/actions/sync")
        enqueued.raise_for_status()
        run_ids = [run["run_id"] for run in enqueued.json()["runs"]]
        sync_runs = wait_for_runs(client, run_ids, timeout_s=args.settle_timeout)
        sync_seconds = time.perf_counter() - started
        failed = [run for run in sync_runs if run["status"] == "failed"]
        if failed:
            raise SystemExit(f"sync failed: {[run['error'] for run in failed]}")
        report["sync"] = {
            "seconds": round(sync_seconds, 1),
            "objects_per_second": round(report["corpus"]["files"] / sync_seconds, 1),
        }

        print("[3/5] triggering pipeline")
        trigger = client.post("/api/actions/pipeline")
        trigger.raise_for_status()
        report["trigger"] = trigger.json()

    started = time.perf_counter()
    final_status = wait_settled(args.app, timeout_s=args.settle_timeout)
    report["pipeline"] = {
        "wall_seconds": round(time.perf_counter() - started, 1),
        "stages": stage_metrics(args.db),
        "counts": final_status.get("counts"),
    }

    print("[4/5] query load")
    report["query_load"] = asyncio.run(
        query_load(args.app, users=args.query_users, per_user=args.queries_per_user)
    )

    print("[5/5] spend + footprint")
    report["spend"] = spend(args.db)
    report["footprint"] = footprint(args.db)

    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["query_load"]["outsider_hits_MUST_BE_ZERO"]:
        raise SystemExit("ETHICAL WALL BREACH UNDER LOAD — outsider received results")


if __name__ == "__main__":
    main()
