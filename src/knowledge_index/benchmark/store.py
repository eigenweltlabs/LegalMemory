"""Frozen gold store — gold is created once and committed into the package.

Gold-label creation (``generate-gold``: LLM proposals + machine verification) is a
one-time job. The result is *frozen* here as a version-controlled artifact under
``data/`` and read by the eval commands on every run; the bulky document corpus stays
regenerable and git-ignored. A frozen set is two files:

- ``<name>.gold.jsonl``  — the gold queries (the benchmark)
- ``<name>.meta.json``   — the corpus config it was frozen against (source, areas,
  matters, docs_target, seed, content_hash, counts), so the exact corpus can be
  reproduced and matched before scoring.

``freeze`` copies a generated gold out of a corpus directory into the store; nothing
regenerates it thereafter.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def gold_path(name: str, *, data_dir: Path | None = None) -> Path:
    return (data_dir or DATA_DIR) / f"{name}.gold.jsonl"


def meta_path(name: str, *, data_dir: Path | None = None) -> Path:
    return (data_dir or DATA_DIR) / f"{name}.meta.json"


def list_frozen(*, data_dir: Path | None = None) -> list[str]:
    directory = data_dir or DATA_DIR
    return sorted(p.name[: -len(".gold.jsonl")] for p in directory.glob("*.gold.jsonl"))


def resolve(name_or_path: str, *, data_dir: Path | None = None) -> Path:
    """Accept either a path to a gold file or the name of a frozen set."""
    candidate = Path(name_or_path)
    if candidate.is_file():
        return candidate
    frozen = gold_path(name_or_path, data_dir=data_dir)
    if frozen.is_file():
        return frozen
    known = list_frozen(data_dir=data_dir)
    raise FileNotFoundError(
        f"no gold file at {name_or_path!r} and no frozen set named that; known: {known}"
    )


def freeze(corpus_dir: str | Path, name: str, *, data_dir: Path | None = None) -> dict:
    """Copy the generated gold from a corpus directory into the committed store."""
    corpus_dir = Path(corpus_dir).resolve()
    directory = data_dir or DATA_DIR
    directory.mkdir(parents=True, exist_ok=True)

    source_gold = corpus_dir / "retrieval-gold.jsonl"
    if not source_gold.is_file():
        raise FileNotFoundError(f"no retrieval-gold.jsonl in {corpus_dir}; run generate-benchmark")
    lines = [line for line in source_gold.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_kind: dict[str, int] = {}
    for line in lines:
        kind = json.loads(line)["kind"]
        by_kind[kind] = by_kind.get(kind, 0) + 1

    shutil.copyfile(source_gold, gold_path(name, data_dir=directory))
    scenario = json.loads((corpus_dir / "scenario.json").read_text(encoding="utf-8"))
    meta = {
        "name": name,
        "source": "harveyai/harvey-labs (MIT)",
        "corpus_config": {
            key: scenario.get(key)
            for key in ("areas", "matters", "scenarios", "documents", "seed", "content_hash")
        },
        "gold_queries": len(lines),
        "by_kind": by_kind,
        "reproduce": (
            f"ki generate-benchmark <out> --source <task-set checkout> "
            f"--areas {','.join(scenario.get('areas', []))} "
            f"--seed {scenario.get('seed')}"
        ),
    }
    meta_path(name, data_dir=directory).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {"gold": str(gold_path(name, data_dir=directory)), **meta}
