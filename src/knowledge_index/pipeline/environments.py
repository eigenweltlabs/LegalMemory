"""RL-environment builder: a separate, sparse, human-in-the-loop flow that proposes
{Legal Task, Input files, Criteria} candidates from the firm's OWN completed work.

This is deliberately NOT part of the insertion pipeline. Auto-generating a benchmark
from every final document is a liability (unfaithful items, contamination, and it
captures whoever's taste happens to be on file). Instead this builder proposes sparse
candidates (``status='proposed'``) gated on firm authorship; a partner approves the few
that genuinely capture the firm's judgment, and only then do they become live holdout
environments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    Document,
    DocumentVersion,
    EvalRecord,
    Matter,
    Relation,
)
from knowledge_index.pipeline.extraction import EVAL_SYSTEM, EvalGeneration
from knowledge_index.pipeline.providers import chat_json, usage_stage
from knowledge_index.taxonomies import EnvironmentStatus, TaskType, VersionStatus


@dataclass
class BuildResult:
    considered: int = 0
    proposed: int = 0
    skipped_external: int = 0
    skipped_ineligible: int = 0
    skipped_capped: int = 0
    errors: int = 0


def _final_text(session: Session, content_hash: str | None) -> str:
    if not content_hash:
        return ""
    artifact = session.scalar(
        select(Artifact)
        .where(Artifact.content_hash == content_hash, Artifact.kind == "structured_json")
        .order_by(Artifact.created_at.desc())
    )
    return str((artifact.payload or {}).get("text") or "") if artifact else ""


def _input_refs(session: Session, document: Document, version: DocumentVersion) -> list[str]:
    """The input files a task actually needs: prior versions of the same document, plus the
    latest versions of documents this one annexes / references / amends / responds to."""
    refs: list[str] = list(
        session.scalars(
            select(DocumentVersion.id)
            .where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.id != version.id,
                DocumentVersion.ordinal < version.ordinal,
            )
            .order_by(DocumentVersion.ordinal)
        ).all()
    )
    related_document_ids = session.scalars(
        select(Relation.to_id).where(
            Relation.from_type == "document",
            Relation.from_id == document.id,
            Relation.to_type == "document",
            Relation.kind.in_(["annex_of", "references", "amends", "responds_to"]),
        )
    ).all()
    for document_id in related_document_ids:
        related = session.get(Document, document_id)
        if related and related.latest_final_version_id:
            refs.append(related.latest_final_version_id)

    seen: set[str] = set()
    ordered: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


class EnvironmentBuilder:
    """Propose RL-environment candidates from firm work product, respecting sparseness caps."""

    def __init__(self, session_factory: sessionmaker, config: AppConfig) -> None:
        self.session_factory = session_factory
        self.config = config

    def build(self, *, limit: int | None = None) -> BuildResult:
        cfg = self.config.environments
        result = BuildResult()
        max_run = min(limit or cfg.max_candidates_per_run, cfg.max_candidates_per_run)
        model = self.config.pipeline.stage("gen_evals").model

        # Environment building is the gen_evals stage even though it runs outside the
        # insertion DAG, so its spend lands under the stage name the cost centre knows.
        with usage_stage("gen_evals"), self.session_factory() as session:
            # Existing per-area counts (proposed + approved) so caps hold across runs.
            per_area: dict[str, int] = {}
            for record in session.scalars(
                select(EvalRecord).where(
                    EvalRecord.status.in_(
                        [EnvironmentStatus.PROPOSED.value, EnvironmentStatus.APPROVED.value]
                    )
                )
            ):
                area = record.practice_area or "other"
                per_area[area] = per_area.get(area, 0) + 1

            candidates = session.scalars(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.status.in_(
                        [VersionStatus.FINAL.value, VersionStatus.EXECUTED.value]
                    ),
                    Document.matter_id.isnot(None),
                )
            ).all()

            for version in candidates:
                if result.proposed >= max_run:
                    break
                document = session.get(Document, version.document_id)
                if document is None or document.latest_final_version_id != version.id:
                    continue
                if (
                    session.scalar(
                        select(EvalRecord).where(EvalRecord.reference_output_ref == version.id)
                    )
                    is not None
                ):
                    continue
                matter = session.get(Matter, document.matter_id) if document.matter_id else None
                area = (matter.practice_area if matter else None) or "other"
                if per_area.get(area, 0) >= cfg.max_per_practice_area:
                    result.skipped_capped += 1
                    continue

                result.considered += 1
                input_refs = _input_refs(session, document, version)
                try:
                    generation = chat_json(
                        model,
                        self.config,
                        system=EVAL_SYSTEM,
                        user=json.dumps(
                            {
                                "document_title": document.title,
                                "doc_type": document.doc_type,
                                "practice_area": area,
                                "parties": document.parties or [],
                                "prior_version_count": len(input_refs),
                                "final_text": _final_text(session, version.content_hash)[:16000],
                            },
                            ensure_ascii=False,
                        ),
                        schema=EvalGeneration,
                    )
                except Exception:
                    result.errors += 1
                    continue

                if not generation.authored_internally:
                    result.skipped_external += 1
                    continue
                if (
                    not generation.eligible
                    or not generation.rubric
                    or not (generation.instruction or "").strip()
                    or generation.confidence < cfg.min_confidence
                ):
                    result.skipped_ineligible += 1
                    continue
                if generation.task_type not in {item.value for item in TaskType}:
                    result.errors += 1
                    continue

                session.add(
                    EvalRecord(
                        matter_id=document.matter_id,
                        task_type=generation.task_type,
                        instruction=generation.instruction,
                        input_refs=input_refs,
                        reference_output_ref=version.id,
                        rubric=[item.model_dump() for item in generation.rubric],
                        verifiers=[verifier.model_dump() for verifier in generation.verifiers],
                        holdout=True,
                        status=EnvironmentStatus.PROPOSED.value,
                        authored_internally=True,
                        practice_area=area,
                        provenance={
                            "model": model,
                            "confidence": generation.confidence,
                            "source": "environment-builder",
                        },
                    )
                )
                per_area[area] = per_area.get(area, 0) + 1
                result.proposed += 1

            session.commit()
        return result
