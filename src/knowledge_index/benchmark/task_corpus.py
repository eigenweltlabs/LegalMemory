"""Shape an open legal-task set into a realistic law-firm DMS tree.

The source dataset (github.com/harveyai/harvey-labs, MIT) ships one folder per task with a
``task.json`` (title, instructions, PASS/FAIL criteria) and a ``documents/`` bundle
of real ``.docx/.xlsx/.eml`` files. This module reads a local checkout and packs
those bundles into a German-firm ``mock_dms/`` tree with the same shape the existing
fixture generator produces — ``mock_dms/`` + ``acl-by-path.json`` + a manifest +
``scenario.json`` — so the standard ``local_fs`` connector ingests it unchanged.

Mapping to the ontology:
- **matter** = one *instrument* (e.g. "Account Control Agreement"), aggregating all
  of its task-type folders and scenarios. The draft / redline / subsequent-redline
  task types on one instrument are its version-chain material.
- **ACL group** = one per practice area, so cross-area retrieval exercises the
  ethical walls; documents from other matters are the retrieval distractors.
- each scenario's ``task.json`` is preserved (title, instructions, criteria) in
  ``scenarios.jsonl`` for gold-label derivation and the phase-2 rubric harness.

Pure filesystem work: no database, no models, deterministic given ``seed``.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

# Task-type suffixes, longest first so the greedy strip is unambiguous.
_TASK_TYPE_SUFFIXES: tuple[str, ...] = (
    "counterparty-paper-review",
    "subsequent-turn-redline",
    "first-turn-redline",
    "playbook-escalation",
    "term-negotiation",
    "first-draft",
    "redline",
    "review",
    "draft",
)


@dataclass
class ScenarioRecord:
    """One packed scenario = one working set inside a matter."""

    scenario_id: str
    matter_ref: str
    matter_title: str
    practice_area: str
    principal: str
    instrument: str
    task_type: str
    title: str
    instructions: str
    criteria: list[dict]
    document_paths: list[str]  # relative to mock_dms, posix


@dataclass
class _Scenario:
    task_json: Path
    documents: list[Path]
    area: str  # posix path under tasks/, e.g. "contracts/banking"
    instrument: str
    task_type: str


def _strip_task_type(name: str) -> tuple[str, str]:
    """Split ``account-control-agreement-first-draft`` into (instrument, task_type)."""
    for suffix in _TASK_TYPE_SUFFIXES:
        if name.endswith("-" + suffix):
            return name[: -(len(suffix) + 1)], suffix
    return name, "task"


def _title_case(slug: str) -> str:
    small = {"and", "of", "the", "for", "to", "in", "a", "an"}
    words = slug.replace("_", "-").split("-")
    return " ".join(w if w in small else w.capitalize() for w in words if w)


def _discover_scenarios(source: Path, areas: list[str]) -> list[_Scenario]:
    tasks_root = source / "tasks"
    if not tasks_root.is_dir():
        raise ValueError(f"not a task-set checkout (missing tasks/): {source}")
    roots = [tasks_root / area for area in areas] if areas else [tasks_root]
    scenarios: list[_Scenario] = []
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"requested area not found: {root}")
        for task_json in sorted(root.rglob("task.json")):
            documents_dir = task_json.parent / "documents"
            if not documents_dir.is_dir():
                continue
            documents = sorted(p for p in documents_dir.iterdir() if p.is_file())
            if not documents:
                continue
            # The dataset contains both layouts:
            #   instrument-tasktype/scenario-NN/task.json
            #   instrument-tasktype/task.json
            # In both cases ``documents/`` is next to task.json.  Treat only an
            # actual scenario directory as the extra nesting level; otherwise the
            # task.json parent is the instrument/task directory itself.
            task_dir = (
                task_json.parent.parent
                if task_json.parent.name.startswith("scenario-")
                else task_json.parent
            )
            area = task_dir.parent.relative_to(tasks_root).as_posix()
            instrument, task_type = _strip_task_type(task_dir.name)
            scenarios.append(
                _Scenario(
                    task_json=task_json,
                    documents=documents,
                    area=area,
                    instrument=f"{area}/{instrument}",
                    task_type=task_type,
                )
            )
    return scenarios


def _grant(principal: str) -> dict:
    return {"principal": principal, "principal_kind": "group", "access": "allow"}


def build_task_corpus(
    output: str | Path,
    source: str | Path,
    *,
    areas: list[str] | None = None,
    matters: int = 50,
    docs_target: int = 1000,
    seed: int = 42,
) -> dict:
    """Pack a task-set checkout into ``output/mock_dms`` and its manifests.

    Selection stops when either the matter cap or the document target is reached,
    whichever comes first; the actual counts are reported in ``scenario.json``.
    """
    output = Path(output).resolve()
    source = Path(source).expanduser().resolve(strict=True)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"benchmark output must be empty: {output}")
    areas = [a.strip() for a in (areas or []) if a.strip()]

    scenarios = _discover_scenarios(source, areas)
    if not scenarios:
        raise ValueError("no scenarios found for the requested areas")

    # group scenarios into matters by instrument; deterministic order, seeded spread
    by_instrument: dict[str, list[_Scenario]] = {}
    for scenario in scenarios:
        by_instrument.setdefault(scenario.instrument, []).append(scenario)
    instruments = sorted(by_instrument)
    random.Random(seed).shuffle(instruments)

    source_root = output / "mock_dms"
    source_root.mkdir(parents=True)
    records: list[ScenarioRecord] = []
    acl_by_path: dict[str, list[dict]] = {}
    matter_refs: list[str] = []
    doc_count = 0

    reached_target = False
    for matter_index, instrument in enumerate(instruments, start=1):
        if matter_index > matters or reached_target:
            break
        group = by_instrument[instrument]
        area = group[0].area
        principal = "group:" + area.replace("/", "-") + "-team"
        instrument_name = instrument.split("/")[-1]
        matter_title = _title_case(instrument_name)
        matter_ref = f"M-2026-{matter_index:04d}"
        matter_refs.append(matter_ref)
        matter_dir = source_root / "Mandate" / f"{matter_ref} {matter_title}"

        for scenario in sorted(group, key=lambda s: (s.task_type, str(s.task_json))):
            scenario_slug = f"{scenario.task_type}-{scenario.task_json.parent.name}"
            scenario_dir = matter_dir / scenario_slug
            scenario_dir.mkdir(parents=True, exist_ok=True)
            document_paths: list[str] = []
            for document in scenario.documents:
                destination = scenario_dir / document.name
                shutil.copyfile(document, destination)
                relative = destination.relative_to(source_root).as_posix()
                document_paths.append(relative)
                acl_by_path[relative] = [_grant(principal)]
                doc_count += 1
            task = json.loads(scenario.task_json.read_text(encoding="utf-8"))
            records.append(
                ScenarioRecord(
                    scenario_id=f"{matter_ref}/{scenario_slug}",
                    matter_ref=matter_ref,
                    matter_title=matter_title,
                    practice_area=area,
                    principal=principal,
                    instrument=instrument,
                    task_type=scenario.task_type,
                    title=task.get("title", ""),
                    instructions=task.get("instructions", ""),
                    criteria=task.get("criteria", []),
                    document_paths=document_paths,
                )
            )
            # scenarios stay atomic (a working set); stop once the target is reached
            if doc_count >= docs_target:
                reached_target = True
                break

    scenarios_path = output / "scenarios.jsonl"
    scenarios_path.write_text(
        "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    acl_path = output / "acl-by-path.json"
    acl_path.write_text(json.dumps(acl_by_path, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "seed": seed,
        "source": str(source),
        "areas": areas or ["<all>"],
        "source_root": str(source_root),
        "scenarios_manifest": str(scenarios_path),
        "acl_by_path": str(acl_path),
        "matters": len(matter_refs),
        "scenarios": len(records),
        "documents": doc_count,
        "matter_refs": matter_refs,
        "principals": sorted({r.principal for r in records}),
        "content_hash": _corpus_hash(records),
    }
    (output / "scenario.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _corpus_hash(records: list[ScenarioRecord]) -> str:
    """Stable digest of the packed layout for run-to-run comparability."""
    digest = hashlib.sha256()
    for record in records:
        digest.update(record.scenario_id.encode("utf-8"))
        for path in record.document_paths:
            digest.update(path.encode("utf-8"))
    return digest.hexdigest()[:16]
