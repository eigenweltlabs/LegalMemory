"""Shape the Harvey LAB task set into a realistic law-firm DMS tree.

Harvey LAB (github.com/harveyai/harvey-labs, MIT) ships one folder per task with a
``task.json`` (title, instructions, PASS/FAIL criteria) and a ``documents/`` bundle
of real ``.docx/.xlsx/.eml`` files. This module reads a local checkout and packs
those bundles into a ``mock_dms/`` tree with the same envelope the existing fixture
generator produces — ``mock_dms/`` + ``acl-by-path.json`` + a manifest +
``scenario.json`` — so the standard ``local_fs`` connector ingests it unchanged.

Two layouts (``build_harvey_corpus(..., layout=...)``):
- **flat** — ``matter = instrument`` (e.g. "Account Control Agreement") with a subfolder
  per scenario, one ACL group per practice area. The draft / redline / subsequent-turn
  task types on one instrument are its version-chain material.
- **firm** — a realistic ``Clients/<client>/<matter>/<workstream>/`` tree: one scenario =
  one matter (named by counterparty), clustered by the represented client with a
  per-client ACL. Client/counterparty naming is a pluggable :data:`PartyResolver`
  (deterministic filenames by default; an LLM resolver for canonical legal names).

Each scenario's ``task.json`` is preserved (title, instructions, criteria) in
``scenarios.jsonl`` for gold-label derivation and the rubric harness. The deterministic
paths are pure filesystem work (no database, no models, reproducible given ``seed``);
only an injected LLM resolver touches the gateway.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from knowledge_index.benchmark import noise

#: Resolve each selected scenario's ``(client, counterparty)`` display names. The
#: default is the deterministic filename guess; an LLM resolver can be injected instead.
PartyResolver = Callable[[list["_Scenario"]], list[tuple[str, str]]]

# Harvey task-type suffixes, longest first so the greedy strip is unambiguous.
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
    """One packed Harvey scenario = one working set inside a matter."""

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
    client: str = ""  # firm layout: the party the firm represents
    counterparty: str = ""  # firm layout: the other side


@dataclass
class _Scenario:
    task_json: Path
    documents: list[Path]
    area: str  # posix path under tasks/, e.g. "contracts/banking"
    instrument: str
    task_type: str
    key: str = ""  # stable id = task path under tasks/, joins a structure manifest


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
        raise ValueError(f"not a Harvey LAB checkout (missing tasks/): {source}")
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
            # Harvey LAB contains both layouts:
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
                    key=task_json.parent.relative_to(tasks_root).as_posix(),
                )
            )
    return scenarios


def _grant(principal: str) -> dict:
    return {"principal": principal, "principal_kind": "group", "access": "allow"}


# --- firm layout: realistic Client / Matter / Workstream tree (deterministic v1) ------

# leading filename tokens that are document-type words, not party names
_GENERIC_TOKENS = {
    "isda",
    "csa",
    "aca",
    "repo",
    "credit",
    "relationship",
    "counterparty",
    "instruction",
    "client",
    "dodd",
    "frank",
    "standard",
    "form",
    "draft",
    "cover",
    "closing",
    "account",
    "financial",
    "deal",
    "master",
    "schedule",
    "confirmation",
    "term",
    "sheet",
    "email",
    "memo",
    "documentation",
    "ibor",
    "precedent",
    "outside",
    "2002",
    "2020",
    "2024",
    "2025",
}

_WORKSTREAM_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Drafts", ("draft", "markup", "turn", "redline")),
    ("Precedents", ("template", "-form", "precedent", "standard-form")),
    (
        "Reference",
        (
            "playbook",
            "term-sheet",
            "memo",
            "requirement",
            "parameter",
            "overview",
            "summary",
            "checklist",
            "ratings",
            "statement",
            "financials",
            "structure",
        ),
    ),
    ("Executed", ("executed", "signed", "-final")),
)


def _classify_workstream(name: str) -> str:
    n = name.lower()
    if n.endswith(".eml"):
        return "Correspondence"
    for folder, keywords in _WORKSTREAM_RULES:
        if any(k in n for k in keywords):
            return folder
    if n.endswith((".xlsx", ".csv")) or "schedule" in n or "account" in n:
        return "Schedules"
    return "Documents"


def _party_stem(filename: str) -> str | None:
    token = filename.rsplit(".", 1)[0].split("-")[0].lower()
    if token in _GENERIC_TOKENS or not token.isalpha() or len(token) < 3:
        return None
    return token


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unassigned"


def _firm_parties(filenames: list[str]) -> tuple[str, str]:
    """Deterministically guess (client, counterparty) from document filenames.

    Client = owner of the *playbook* (a firm keeps its client's playbook), else the party
    prefixing the most documents. Counterparty = owner of a *draft* (they circulated it),
    else the next most common party. Names are the filename stem, title-cased — good enough
    for a first cut; an LLM pass can resolve full legal names + merge variants later.
    """
    stems = [s for s in (_party_stem(n) for n in filenames) if s]
    if not stems:
        return ("Unassigned", "")
    counts = Counter(stems)
    playbook = next((_party_stem(n) for n in filenames if "playbook" in n.lower()), None)
    client = playbook if playbook in counts else counts.most_common(1)[0][0]
    draft = next(
        (
            _party_stem(n)
            for n in filenames
            if any(w in n.lower() for w in ("draft", "markup", "turn"))
            and _party_stem(n) not in (None, client)
        ),
        None,
    )
    counterparty = draft or next((s for s, _ in counts.most_common() if s != client), "")
    return (_title_case(client), _title_case(counterparty) if counterparty else "")


def _deterministic_parties(scenarios: list[_Scenario]) -> list[tuple[str, str]]:
    """The free default :data:`PartyResolver`: a filename-stem guess per scenario."""
    return [_firm_parties([d.name for d in s.documents]) for s in scenarios]


def _norm_client(name: str) -> str:
    return (name or "Unassigned").replace("/", "-").strip() or "Unassigned"


def _norm_title(title: str) -> str:
    # matter titles become folder names; a '/' would spawn a spurious subfolder
    return (title or "").replace("/", "-").strip()


def _auto_matter_no(client: str, seq: dict[str, int]) -> str:
    slug = _slug(client)
    seq[slug] = seq.get(slug, 0) + 1
    return f"{slug[:3].upper()}-{seq[slug]:03d}"


def _auto_matter_title(scenario: _Scenario, counterparty: str) -> str:
    instrument = _title_case(scenario.instrument.split("/")[-1])
    return f"{instrument} — {counterparty}" if counterparty else instrument


def _plan_matters(
    selected: list[_Scenario], resolve_parties: PartyResolver, structure: dict | None
) -> list[dict]:
    """One ``{client, matter_no, counterparty, matter_title}`` per selected scenario.

    A ``structure`` manifest (keyed by ``scenario.key``) is authoritative — its curated
    client / matter number / counterparty / title are used verbatim, so the firm is a
    committed artifact, not build-time inference. Scenarios missing from the manifest, or
    the no-manifest path, fall back to ``resolve_parties`` + auto numbering/titles.
    """
    seq: dict[str, int] = {}
    if structure is not None:
        by_key = structure.get("matters", {})
        plans: list[dict] = []
        for scenario in selected:
            record = by_key.get(scenario.key)
            if record:
                client = _norm_client(record.get("client"))
                counterparty = _norm_title(record.get("counterparty"))
                plans.append(
                    {
                        "client": client,
                        "matter_no": record.get("matter_no") or _auto_matter_no(client, seq),
                        "counterparty": counterparty,
                        "matter_title": _norm_title(record.get("matter_title"))
                        or _auto_matter_title(scenario, counterparty),
                    }
                )
            else:
                client, counterparty = _firm_parties([d.name for d in scenario.documents])
                client = _norm_client(client)
                counterparty = _norm_title(counterparty)
                plans.append(
                    {
                        "client": client,
                        "matter_no": _auto_matter_no(client, seq),
                        "counterparty": counterparty,
                        "matter_title": _auto_matter_title(scenario, counterparty),
                    }
                )
        return plans

    parties = resolve_parties(selected)
    if len(parties) != len(selected):
        raise ValueError(
            f"party resolver returned {len(parties)} names for {len(selected)} scenarios"
        )
    plans = []
    for scenario, (client, counterparty) in zip(selected, parties, strict=True):
        client = _norm_client(client)
        counterparty = _norm_title(counterparty)
        plans.append(
            {
                "client": client,
                "matter_no": _auto_matter_no(client, seq),
                "counterparty": counterparty,
                "matter_title": _auto_matter_title(scenario, counterparty),
            }
        )
    return plans


def _build_firm_corpus(
    output: Path,
    source: Path,
    scenarios: list[_Scenario],
    *,
    matters: int,
    docs_target: int,
    seed: int,
    areas: list[str],
    resolve_parties: PartyResolver = _deterministic_parties,
    structure: dict | None = None,
    noise_config: noise.NoiseConfig | None = None,
) -> dict:
    """Pack scenarios as a realistic firm DMS: Clients / Matter / Workstream (one
    scenario = one matter), clustered by the represented client with a per-client ACL.

    Runs in three stages so matter identity is a single pluggable step: **select** the
    scenarios that fit under the matter cap / document target, **plan** each one's
    ``{client, matter_no, counterparty, matter_title}`` (a curated ``structure`` manifest
    if given, else ``resolve_parties`` + auto numbering), then **pack** the tree.

    ``noise_config`` opts into realistic DMS mess (flat/renamed folders, extra document
    versions, junk); it is deterministic per matter and never moves a gold path across a
    wall (see :mod:`~knowledge_index.benchmark.noise`).
    """
    source_root = output / "mock_dms"
    source_root.mkdir(parents=True)
    ordered = sorted(scenarios, key=lambda s: (s.instrument, s.task_type, str(s.task_json)))
    random.Random(seed).shuffle(ordered)

    # 1) select — scenarios stay atomic (a whole working set) up to the caps
    selected: list[_Scenario] = []
    doc_budget = 0
    for scenario in ordered:
        if len(selected) >= matters or doc_budget >= docs_target:
            break
        selected.append(scenario)
        doc_budget += len(scenario.documents)

    # 2) plan each matter's identity (curated manifest, or resolver + auto numbering)
    plans = _plan_matters(selected, resolve_parties, structure)

    # 3) pack the tree
    records: list[ScenarioRecord] = []
    acl_by_path: dict[str, list[dict]] = {}
    doc_count = 0
    noise_stats = {"flat": 0, "alt": 0, "junk": 0}

    for scenario, plan in zip(selected, plans, strict=True):
        task = json.loads(scenario.task_json.read_text(encoding="utf-8"))
        client = plan["client"]
        counterparty = plan["counterparty"]
        matter_no = plan["matter_no"]
        matter_title = plan["matter_title"]
        principal = f"group:{_slug(client)}"
        matter_dir = source_root / "Clients" / client / f"{matter_no}  {matter_title}"

        style = noise.matter_style(noise_config, seed, matter_no) if noise_config else "standard"
        if noise_config and style in ("flat", "alt"):
            noise_stats[style] += 1

        document_paths: list[str] = []
        for document in scenario.documents:
            workstream = _classify_workstream(document.name)
            subfolder = noise.place(workstream, style) if noise_config else workstream
            target_dir = matter_dir / subfolder if subfolder else matter_dir
            destination = target_dir / document.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(document, destination)
            relative = destination.relative_to(source_root).as_posix()
            document_paths.append(relative)
            acl_by_path[relative] = [_grant(principal)]
            doc_count += 1

        # junk: walled (no wall leak) but not a document — quarantined at extraction
        if noise_config and noise.wants(noise_config.junk_rate, seed, matter_no, "junk"):
            for junk in noise.scatter_junk(matter_dir, seed, matter_no):
                acl_by_path[junk.relative_to(source_root).as_posix()] = [_grant(principal)]
                noise_stats["junk"] += 1

        records.append(
            ScenarioRecord(
                scenario_id=f"{matter_no}/{scenario.task_type}-{scenario.task_json.parent.name}",
                matter_ref=matter_no,
                matter_title=matter_title,
                practice_area=scenario.area,
                principal=principal,
                instrument=scenario.instrument,
                task_type=scenario.task_type,
                title=task.get("title", ""),
                instructions=task.get("instructions", ""),
                criteria=task.get("criteria", []),
                document_paths=document_paths,
                client=client,
                counterparty=counterparty,
            )
        )

    (output / "scenarios.jsonl").write_text(
        "".join(json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )
    (output / "acl-by-path.json").write_text(
        json.dumps(acl_by_path, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    clients = sorted({r.client for r in records})
    summary = {
        "seed": seed,
        "source": str(source),
        "areas": areas or ["<all>"],
        "layout": "firm",
        "source_root": str(source_root),
        "scenarios_manifest": str(output / "scenarios.jsonl"),
        "acl_by_path": str(output / "acl-by-path.json"),
        "clients": len(clients),
        "matters": len(records),
        "scenarios": len(records),
        "documents": doc_count,
        "client_names": clients,
        "principals": sorted({r.principal for r in records}),
        "content_hash": _corpus_hash(records),
    }
    if noise_config is not None:
        summary["noise"] = noise_stats
    (output / "scenario.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def build_harvey_corpus(
    output: str | Path,
    source: str | Path,
    *,
    areas: list[str] | None = None,
    matters: int = 50,
    docs_target: int = 1000,
    seed: int = 42,
    layout: str = "flat",
    resolve_parties: PartyResolver | None = None,
    structure: str | Path | dict | None = None,
    noise_level: str | None = None,
) -> dict:
    """Pack a Harvey checkout into ``output/mock_dms`` and its manifests.

    ``layout``: ``"flat"`` (matter = instrument, scenario subfolders) or ``"firm"`` (a
    realistic Client / Matter / Workstream tree, one scenario = one matter, per-client
    ACL). Selection stops at the matter cap or the document target, whichever comes first.

    Firm layout, matter identity (choose one):
    - ``structure`` — a curated manifest (path or dict) mapping ``scenario.key`` to
      ``{client, matter_no, counterparty, matter_title}``; applied verbatim so the firm is
      a committed, reproducible artifact. This is the recommended path.
    - ``resolve_parties`` — name ``(client, counterparty)`` per matter at build time
      (deterministic filename guess by default, or an LLM resolver). Used only where a
      manifest is absent.

    ``noise_level`` (firm layout, ``"light"``/``"heavy"``) opts into realistic DMS mess —
    flat/renamed folders, extra document versions, junk — deterministically and gold-safe.
    """
    output = Path(output).resolve()
    source = Path(source).expanduser().resolve(strict=True)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"benchmark output must be empty: {output}")
    areas = [a.strip() for a in (areas or []) if a.strip()]
    if isinstance(structure, (str, Path)):
        structure = json.loads(Path(structure).expanduser().read_text(encoding="utf-8"))
    noise_config = noise.resolve(noise_level)

    scenarios = _discover_scenarios(source, areas)
    if not scenarios:
        raise ValueError("no Harvey scenarios found for the requested areas")

    if layout == "firm":
        return _build_firm_corpus(
            output,
            source,
            scenarios,
            matters=matters,
            docs_target=docs_target,
            seed=seed,
            areas=areas,
            resolve_parties=resolve_parties or _deterministic_parties,
            structure=structure,
            noise_config=noise_config,
        )
    if layout != "flat":
        raise ValueError(f"unknown layout {layout!r}; use 'flat' or 'firm'")
    if resolve_parties is not None or structure is not None or noise_config is not None:
        raise ValueError("resolve_parties/structure/noise apply only to the firm layout")

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
