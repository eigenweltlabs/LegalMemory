"""Rewrite a frozen gold file onto a corpus that holds the same documents under
different paths.

``full.gold.jsonl`` was frozen against the ``Mandate/`` corpus emitted by
``task_corpus.build_mandate_corpus``: paths shaped
``Mandate/M-2026-0609 <title>/<scenario-slug>/<file>``, ACLs granted to
``group:<practice-area>-team``. The corpus ingested for the 50k run is the
``Clients/`` corpus instead: ``Clients/<client>/<REF-NNN> <title>/<folder>/<file>``
with ACLs granted to ``group:<client-slug>``. Both generators emit the *same
document set*, so every gold filename exists on both sides, but nothing else
lines up.

That mismatch is not cosmetic. ``retrieval_eval.corpus_coverage`` matches gold
paths against ``SourceObject.path`` exactly and refuses to score at anything short
of full coverage, and ``principals`` feeds the ACL filter, so a stale principal
returns zero hits on every query rather than failing loudly.

This script rewrites both, keying on the one thing the two corpora share: the
filename. It is deliberately conservative, because a gold file that scores the
wrong document is worse than a smaller one:

* a filename must resolve to **exactly one** corpus path, so ambiguous names
  (``board-minutes.docx`` and friends, which recur across matters) are dropped
  rather than guessed at;
* every document behind a record must sit under **one** principal, since a caller
  holding a single client group cannot see documents split across clients;
* with ``--verify-answers`` (default), a record survives only if its gold answer
  string is actually present in the converted text of one of its gold documents.
  The two corpora generate party and firm names independently, so a filename can
  match while the facts inside it do not.

Records that survive keep their original ``id`` for traceability, and stash the
pre-remap matter ref and principals under ``meta.original_*``.

Usage::

    python scripts/remap_gold_to_corpus.py \
        --gold src/knowledge_index/benchmark/data/full.gold.jsonl \
        --out src/knowledge_index/benchmark/data/full.gold.clients.jsonl

Requires ``KI_DATABASE_URL`` pointing at the ingested corpus.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from knowledge_index.db.engine import get_session
from knowledge_index.db.models import (
    Artifact,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    SourceObject,
    SourceObjectGrant,
)


def referenced_paths(record: dict) -> list[str]:
    """Every corpus path a gold record points at, scoring and metadata alike.

    ``meta.primary_path`` / ``meta.secondary_paths`` are remapped too: leaving them
    on the old corpus would quietly desynchronise the metadata from the paths the
    harness actually scores.
    """
    meta = record.get("meta") or {}
    paths = list(record.get("gold_paths") or [])
    if meta.get("primary_path"):
        paths.append(meta["primary_path"])
    paths.extend(meta.get("secondary_paths") or [])
    return paths


def corpus_index(session, basenames: set[str]) -> dict[str, dict[str, dict]]:
    """Map ``basename -> path -> {principals, matter_refs}`` for the ingested corpus.

    Grants come from ``SourceObjectGrant`` rather than ``DocumentGrant`` because the
    appliance runs ``source_acl_mode="sufficient"``: the mirrored source ACL is what
    actually makes a document visible, and the document-level grant table is empty
    for a plain filesystem sync.
    """
    rows = session.execute(
        select(
            SourceObject.name,
            SourceObject.path,
            SourceObjectGrant.principal,
            Matter.reference_numbers,
        )
        .select_from(SourceObject)
        .outerjoin(
            SourceObjectGrant,
            (SourceObjectGrant.source_object_id == SourceObject.id)
            & (SourceObjectGrant.effect == "allow"),
        )
        .outerjoin(
            DocumentVersionSource,
            DocumentVersionSource.source_object_id == SourceObject.id,
        )
        .outerjoin(DocumentVersion, DocumentVersion.id == DocumentVersionSource.version_id)
        .outerjoin(Document, Document.id == DocumentVersion.document_id)
        .outerjoin(Matter, Matter.id == Document.matter_id)
        .where(SourceObject.name.in_(basenames), SourceObject.deleted_at.is_(None))
    ).all()

    index: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"principals": set(), "matter_refs": set()}
    ))
    for name, path, principal, refs in rows:
        entry = index[name][path]
        if principal:
            entry["principals"].add(principal)
        for ref in refs or []:
            entry["matter_refs"].add(ref)
    return index


def document_text(session, path: str) -> str:
    """Converted text for a corpus path, or '' when nothing has been converted yet."""
    source = session.scalar(select(SourceObject).where(SourceObject.path == path))
    if source is None:
        return ""
    artifact = session.scalar(
        select(Artifact)
        .where(Artifact.content_hash == source.content_hash, Artifact.kind == "structured_json")
        .order_by(Artifact.created_at.desc())
    )
    return ((artifact.payload or {}).get("text") if artifact else "") or ""


def remap(gold: list[dict], session, *, verify_answers: bool) -> tuple[list[dict], dict[str, int]]:
    basenames = {p.rsplit("/", 1)[-1] for record in gold for p in referenced_paths(record)}
    index = corpus_index(session, basenames)
    unique = {name: next(iter(paths)) for name, paths in index.items() if len(paths) == 1}

    stats: dict[str, int] = defaultdict(int)
    kept: list[dict] = []
    for record in gold:
        if not record.get("gold_paths"):
            stats["no_gold_paths"] += 1
            continue
        names = [p.rsplit("/", 1)[-1] for p in referenced_paths(record)]
        if not all(name in unique for name in names):
            stats["ambiguous_or_missing"] += 1
            continue

        principals: set[str] = set()
        matter_refs: set[str] = set()
        for name in names:
            entry = index[name][unique[name]]
            principals |= entry["principals"]
            matter_refs |= entry["matter_refs"]
        if not principals:
            stats["no_principal"] += 1
            continue
        if len(principals) > 1:
            stats["split_across_clients"] += 1
            continue

        remapped = dict(record)
        meta = dict(record.get("meta") or {})
        remapped["gold_paths"] = [unique[p.rsplit("/", 1)[-1]] for p in record["gold_paths"]]
        if meta.get("primary_path"):
            meta["primary_path"] = unique[meta["primary_path"].rsplit("/", 1)[-1]]
        if meta.get("secondary_paths"):
            meta["secondary_paths"] = [
                unique[p.rsplit("/", 1)[-1]] for p in meta["secondary_paths"]
            ]

        answer = " ".join((meta.get("answer") or "").split())
        if verify_answers and answer and len(answer) <= 60:
            texts = [document_text(session, p) for p in remapped["gold_paths"]]
            if not any(answer.lower() in text.lower() for text in texts):
                stats["answer_not_in_corpus"] += 1
                continue
            stats["answer_verified"] += 1
        else:
            stats["answer_unchecked"] += 1

        meta["remapped_from_corpus"] = "Mandate"
        meta["original_matter_ref"] = record.get("matter_ref")
        meta["original_principals"] = record.get("principals")
        remapped["meta"] = meta
        remapped["principals"] = sorted(principals)
        if matter_refs:
            remapped["matter_ref"] = sorted(matter_refs)[0]
        kept.append(remapped)
        stats["kept"] += 1
    return kept, dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True, help="frozen gold jsonl to remap")
    parser.add_argument("--out", type=Path, required=True, help="where to write the remapped gold")
    parser.add_argument(
        "--no-verify-answers",
        dest="verify_answers",
        action="store_false",
        help="skip the content check (faster, but keeps records whose facts moved)",
    )
    args = parser.parse_args()

    gold = [
        json.loads(line)
        for line in args.gold.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with get_session() as session:
        kept, stats = remap(gold, session, verify_answers=args.verify_answers)

    args.out.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in kept),
        encoding="utf-8",
    )

    print(f"read {len(gold)} gold records from {args.gold}")
    for reason in (
        "kept",
        "ambiguous_or_missing",
        "split_across_clients",
        "answer_not_in_corpus",
        "no_principal",
        "no_gold_paths",
    ):
        if stats.get(reason):
            print(f"  {reason:22s} {stats[reason]}")
    print(f"wrote {len(kept)} records to {args.out}")


if __name__ == "__main__":
    main()
