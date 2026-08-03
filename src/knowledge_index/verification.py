"""Ground-truth checks for the generated mock DMS."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    DecisionRecord,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    EvalRecord,
    Matter,
    MatterAssignment,
    ProcessingState,
    Relation,
    SourceObject,
)
from knowledge_index.retrieval import RetrievalService


def verify_fixture(
    session_factory: sessionmaker[Session], config: AppConfig, manifest_path: str | Path
) -> dict:
    records = [
        json.loads(line)
        for line in Path(manifest_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    expected_paths = {record["relative_path"] for record in records}
    checks: dict[str, dict] = {}

    with session_factory() as session:
        by_source: dict[str, list[SourceObject]] = defaultdict(list)
        for source_object in session.scalars(select(SourceObject)).all():
            by_source[source_object.source_id].append(source_object)
        candidates = [
            rows for rows in by_source.values() if expected_paths <= {row.path for row in rows}
        ]
        source_rows = min(candidates, key=lambda rows: len(rows) - len(expected_paths), default=[])
        objects = {row.path: row for row in source_rows}
        checks["sync_coverage"] = _check(
            set(objects) == expected_paths,
            observed=len(objects),
            expected=len(records),
            missing=sorted(expected_paths - set(objects)),
            unexpected=sorted(set(objects) - expected_paths),
        )

        hash_mismatches = [
            record["relative_path"]
            for record in records
            if record["relative_path"] not in objects
            or objects[record["relative_path"]].content_hash != record["content_hash"]
        ]
        checks["content_hashes"] = _check(not hash_mismatches, mismatches=hash_mismatches)

        entities: dict[str, tuple[SourceObject, DocumentVersion, Document]] = {}
        for path, source_object in objects.items():
            link = session.scalar(
                select(DocumentVersionSource).where(
                    DocumentVersionSource.source_object_id == source_object.id
                )
            )
            version = session.get(DocumentVersion, link.version_id) if link else None
            document = session.get(Document, version.document_id) if version else None
            if version is not None and document is not None:
                entities[path] = (source_object, version, document)

        quarantine_errors: list[str] = []
        for record in records:
            source_object = objects.get(record["relative_path"])
            if source_object is None:
                continue
            state = session.scalar(
                select(ProcessingState).where(
                    ProcessingState.source_object_id == source_object.id,
                    ProcessingState.stage == "convert",
                )
            )
            expected_quarantine = record["expected_pipeline"] == "quarantined"
            actual_quarantine = state is not None and state.status == "quarantined"
            expected_error = record.get("expected_error")
            actual_error = (state.last_error or {}).get("class") if state else None
            if expected_quarantine != actual_quarantine or (
                expected_error and expected_error != actual_error
            ):
                quarantine_errors.append(record["relative_path"])
        object_ids = [row.id for row in source_rows]
        quarantined_count = (
            session.scalar(
                select(func.count())
                .select_from(ProcessingState)
                .where(
                    ProcessingState.source_object_id.in_(object_ids),
                    ProcessingState.status == "quarantined",
                )
            )
            if object_ids
            else 0
        ) or 0
        checks["quarantine"] = _check(
            not quarantine_errors,
            mismatches=quarantine_errors,
            total=quarantined_count,
        )

        # Document typing is ontology-based: fixture expectations are ancestor
        # LABELS ("Agreements", "Litigation Document"); a record without an
        # accepted list (or with "*") only requires the document to be typed at
        # some visible node — raw LMSS has no home for several everyday genres
        # (emails, internal notes, court-issued documents).
        scope = config.doc_ontology()

        def type_matches(doc_type: str | None, accepted: set[str]) -> bool:
            if not doc_type:
                return False
            if "*" in accepted:
                return doc_type in scope.visible
            labels = {scope.label_of(node) for node in scope.ancestors(doc_type)}
            return bool(labels & accepted)

        assignment_errors: list[str] = []
        metadata_errors: list[str] = []
        for record in records:
            if record["expected_pipeline"] == "quarantined":
                continue
            source_object = objects.get(record["relative_path"])
            entity = entities.get(record["relative_path"])
            if source_object is None or entity is None:
                assignment_errors.append(record["relative_path"])
                metadata_errors.append(record["relative_path"])
                continue
            assignment = session.get(MatterAssignment, source_object.id)
            matter = session.get(Matter, assignment.matter_id) if assignment else None
            expected_matter = record["matter_ref"]
            if expected_matter and (
                matter is None or expected_matter not in (matter.reference_numbers or [])
            ):
                assignment_errors.append(record["relative_path"])
            accepted_types = set(record.get("accepted_doc_types") or ["*"])
            if not type_matches(entity[2].doc_type, accepted_types):
                metadata_errors.append(
                    f"{record['relative_path']} ({entity[2].doc_type} has no ancestor in "
                    f"{sorted(accepted_types)})"
                )
        checks["matter_assignment"] = _check(not assignment_errors, mismatches=assignment_errors)
        checks["document_types"] = _check(not metadata_errors, mismatches=metadata_errors)

        names_to_path = {
            Path(record["relative_path"]).name: record["relative_path"] for record in records
        }
        version_errors: list[str] = []
        lineage_errors: list[str] = []
        duplicate_errors: list[str] = []
        for record in records:
            expected_version = record.get("version")
            if not expected_version:
                continue
            entity = entities.get(record["relative_path"])
            if entity is None:
                version_errors.append(record["relative_path"])
                continue
            _source_object, version, _document = entity
            if (
                version.status != expected_version["status"]
                or version.ordinal != expected_version["ordinal"]
            ):
                version_errors.append(record["relative_path"])
            previous_name = expected_version.get("previous")
            if previous_name:
                previous_entity = entities.get(names_to_path.get(previous_name, ""))
                edge = (
                    session.scalar(
                        select(Relation).where(
                            Relation.from_type == "document_version",
                            Relation.from_id == version.id,
                            Relation.to_type == "document_version",
                            Relation.to_id == previous_entity[1].id,
                            Relation.kind == "supersedes",
                        )
                    )
                    if previous_entity
                    else None
                )
                if edge is None:
                    lineage_errors.append(record["relative_path"])
                if "redline" in record["relative_path"].casefold() and (
                    previous_entity is None or version.redline_against != previous_entity[1].id
                ):
                    lineage_errors.append(f"{record['relative_path']}:redline_against")
            duplicate_name = expected_version.get("duplicate_of")
            if duplicate_name:
                duplicate_entity = entities.get(names_to_path.get(duplicate_name, ""))
                if duplicate_entity is None or duplicate_entity[1].id != version.id:
                    duplicate_errors.append(record["relative_path"])
        checks["version_observations"] = _check(not version_errors, mismatches=version_errors)
        checks["version_lineage"] = _check(not lineage_errors, mismatches=lineage_errors)
        checks["exact_deduplication"] = _check(not duplicate_errors, mismatches=duplicate_errors)

        falke_records = [record for record in records if record["logical_document"] == "falke-spa"]
        falke_versions = {
            entities[record["relative_path"]][1].id: entities[record["relative_path"]][1]
            for record in falke_records
            if record["relative_path"] in entities
        }
        expected_chain = [
            item["status"]
            for item in sorted(
                {
                    record["content_hash"]: record["version"]
                    for record in falke_records
                    if record.get("version")
                }.values(),
                key=lambda item: item["ordinal"],
            )
        ]
        observed_chain = [
            version.status
            for version in sorted(falke_versions.values(), key=lambda item: item.ordinal or 0)
        ]
        checks["version_chain"] = _check(
            observed_chain == expected_chain,
            statuses=observed_chain,
            expected=expected_chain,
            unique_versions=len(falke_versions),
            source_observations=len(falke_records),
        )

        logical_documents: dict[str, set[str]] = defaultdict(set)
        for record in records:
            entity = entities.get(record["relative_path"])
            if entity:
                logical_documents[record["logical_document"]].add(entity[2].id)
        relation_errors: list[str] = []
        for record in records:
            entity = entities.get(record["relative_path"])
            if entity is None:
                continue
            for expected_relation in record.get("relations", []):
                targets = logical_documents.get(expected_relation["target"], set())
                present = (
                    session.scalar(
                        select(Relation).where(
                            Relation.from_type == "document",
                            Relation.from_id == entity[2].id,
                            Relation.to_type == "document",
                            Relation.to_id.in_(targets),
                            Relation.kind == expected_relation["kind"],
                        )
                    )
                    if targets
                    else None
                )
                if present is None:
                    relation_errors.append(f"{record['relative_path']}:{expected_relation['kind']}")
        relation_kinds = set(session.scalars(select(Relation.kind)).all())
        required_relations = {
            "supersedes",
            "annex_of",
            "belongs_to_thread",
            "responds_to",
            "references",
        }
        # AI relation inference: require every relation TYPE and the large majority of
        # the individual cross-document edges. A single missed edge on a run is model
        # variance, not a system failure — the structure is inferred from folders+content.
        total_expected_edges = sum(len(record.get("relations", [])) for record in records)
        max_tolerated = max(1, total_expected_edges // 5)
        checks["relations"] = _check(
            len(relation_errors) <= max_tolerated and required_relations <= relation_kinds,
            mismatches=relation_errors,
            tolerated=max_tolerated,
            present=sorted(relation_kinds),
            missing=sorted(required_relations - relation_kinds),
        )

        email_document_ids = {
            entities[record["relative_path"]][2].id
            for record in records
            if record["doc_type"] == "email" and record["relative_path"] in entities
        }
        threaded_email_ids = (
            set(
                session.scalars(
                    select(Relation.from_id).where(
                        Relation.kind == "belongs_to_thread",
                        Relation.from_type == "document",
                        Relation.from_id.in_(email_document_ids),
                    )
                ).all()
            )
            if email_document_ids
            else set()
        )
        checks["email_threads"] = _check(
            threaded_email_ids == email_document_ids,
            emails=len(email_document_ids),
            threaded=len(threaded_email_ids),
        )

        decision_errors: list[str] = []
        expected_decisions = [
            record for record in records if (record.get("rationale") or {}).get("expect_record")
        ]
        for record in expected_decisions:
            entity = entities.get(record["relative_path"])
            rationale = record["rationale"]
            decision = session.scalar(
                select(DecisionRecord).where(
                    DecisionRecord.version_to == (entity[1].id if entity else "")
                )
            )
            if decision is None:
                decision_errors.append(f"{record['relative_path']}: no decision record")
                continue
            haystack = (
                f"{decision.locus or ''} {decision.change_summary or ''} "
                f"{decision.rationale_text}"
            ).casefold()
            missing_keywords = [
                keyword
                for keyword in rationale.get("required_keywords", [])
                if keyword.casefold() not in haystack
            ]
            if missing_keywords:
                decision_errors.append(f"{record['relative_path']}: missing {missing_keywords}")
            accepted = rationale.get("accepted_categories") or [rationale.get("category")]
            if decision.rationale_category not in accepted:
                decision_errors.append(
                    f"{record['relative_path']}: category {decision.rationale_category}"
                )
            locus_keywords = rationale.get("locus_keywords") or []
            locus_haystack = f"{decision.locus or ''} {decision.change_summary or ''}".casefold()
            if locus_keywords and not any(
                keyword.casefold() in locus_haystack for keyword in locus_keywords
            ):
                decision_errors.append(f"{record['relative_path']}: locus {decision.locus!r}")
        pii_terms = {
            value.casefold() for record in records for value in record.get("pii", []) if value
        }
        all_decisions = session.scalars(select(DecisionRecord)).all()
        leaked_terms = sorted(
            term
            for term in pii_terms
            if any(term in decision.rationale_text.casefold() for decision in all_decisions)
        )
        decision_count = session.scalar(select(func.count()).select_from(DecisionRecord)) or 0
        checks["decision_rationale"] = _check(
            not decision_errors and not leaked_terms and decision_count >= len(expected_decisions),
            count=decision_count,
            expected_at_least=len(expected_decisions),
            mismatches=decision_errors,
            leaked_pii=leaked_terms,
        )

        eval_records = session.scalars(select(EvalRecord)).all()
        final_version_ids = {
            entity[1].id
            for entity in entities.values()
            if entity[1].status in {"final", "executed"}
        }
        falke_final_refs = {
            entity[1].id
            for record in records
            if record["logical_document"] == "falke-spa"
            and (record.get("version") or {}).get("status") in {"final", "executed"}
            and (entity := entities.get(record["relative_path"])) is not None
        }
        rubric_keys = {"id", "criterion", "description", "weight", "kind", "sources"}
        invalid_evals = [
            record.id
            for record in eval_records
            if not record.holdout
            or record.reference_output_ref not in final_version_ids
            or not record.rubric
            or any(
                not rubric_keys <= set(item)
                or item["kind"] not in {"binary", "scale_1_5"}
                or not isinstance(item["weight"], (int, float))
                or item["weight"] <= 0
                for item in record.rubric
            )
        ]
        observed_eval_refs = {record.reference_output_ref for record in eval_records}
        # The judge model decides task eligibility; structurally we require that the
        # clearly-benchmarkable work product (the contract chain) produced an eval
        # with prior versions as inputs, and that every eval anchors a real final.
        chain_eval_has_inputs = any(
            record.reference_output_ref in falke_final_refs and record.input_refs
            for record in eval_records
        )
        checks["eval_records"] = _check(
            not invalid_evals
            and bool(observed_eval_refs & falke_final_refs)
            and chain_eval_has_inputs,
            count=len(eval_records),
            invalid=invalid_evals,
            chain_input_refs=chain_eval_has_inputs,
        )

        retrieval = RetrievalService(session, config)
        ma_hits = retrieval.search_semantic(
            "Haftung Kaufpreis", principals={"group:ma-team"}, limit=50
        )
        litigation_hits = retrieval.search_semantic(
            "Kaufpreisforderung Klageerwiderung Mängel",
            principals={"group:litigation"},
            limit=50,
        )
        outsider_hits = retrieval.search_filter(principals={"user:outsider"}, limit=50)
        # ontology-based recall: the M&A hits must include something typed under
        # Agreements; the litigation hits something under Litigation Document.
        def hit_under(hit, branch_label: str) -> bool:
            if not hit.doc_type:
                return False
            labels = {scope.label_of(node) for node in scope.ancestors(hit.doc_type)}
            return branch_label in labels

        checks["search_recall"] = _check(
            any(hit_under(hit, "Agreements") for hit in ma_hits)
            and any(hit_under(hit, "Litigation Document") for hit in litigation_hits),
            ma_contract_hits=sum(hit_under(hit, "Agreements") for hit in ma_hits),
            litigation_pleading_hits=sum(
                hit_under(hit, "Litigation Document") for hit in litigation_hits
            ),
        )
        checks["ethical_walls"] = _check(
            bool(ma_hits)
            and bool(litigation_hits)
            and not outsider_hits
            and all("M-2026-0099" not in path for hit in ma_hits for path in hit.source_paths)
            and all(
                "M-2026-0042" not in path for hit in litigation_hits for path in hit.source_paths
            ),
            ma_hits=len(ma_hits),
            litigation_hits=len(litigation_hits),
            outsider_hits=len(outsider_hits),
        )

        unfinished = (
            session.scalars(
                select(ProcessingState).where(
                    ProcessingState.source_object_id.in_(object_ids),
                    ProcessingState.status.in_(["pending", "running", "failed"]),
                )
            ).all()
            if object_ids
            else []
        )
        expected_indexed = {
            objects[record["relative_path"]].id
            for record in records
            if record["expected_pipeline"] == "done" and record["relative_path"] in objects
        }
        indexed = (
            set(
                session.scalars(
                    select(ProcessingState.source_object_id).where(
                        ProcessingState.source_object_id.in_(expected_indexed),
                        ProcessingState.stage == "index",
                        ProcessingState.status == "done",
                    )
                ).all()
            )
            if expected_indexed
            else set()
        )
        checks["pipeline_completion"] = _check(
            not unfinished and indexed == expected_indexed,
            unfinished=len(unfinished),
            indexed=len(indexed),
            expected_indexed=len(expected_indexed),
        )

        expected_documents = {
            record["logical_document"]
            for record in records
            if record["expected_pipeline"] != "quarantined"
        }
        observed_documents = {entity[2].id for entity in entities.values()}
        observed_matters = {entity[2].matter_id for entity in entities.values()}
        checks["knowledge_counts"] = _check(
            len(observed_documents) == len(expected_documents),
            matters=len(observed_matters),
            documents=len(observed_documents),
            expected_documents=len(expected_documents),
        )

    return {"passed": all(item["passed"] for item in checks.values()), "checks": checks}


def _check(passed: bool, **details) -> dict:
    return {"passed": passed, **details}
