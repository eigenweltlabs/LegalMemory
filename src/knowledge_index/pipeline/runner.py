"""Durable, idempotent per-document pipeline runner."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.artifacts import ArtifactTooLarge, LocalArtifactStore
from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    Blob,
    Chunk,
    Client,
    CommunicationThread,
    DecisionRecord,
    Document,
    DocumentGrant,
    DocumentVersion,
    DocumentVersionSource,
    Extraction,
    Matter,
    MatterAssignment,
    MatterClient,
    MatterParty,
    Party,
    ProcessingState,
    Project,
    ProjectGrant,
    Relation,
    RelationIntent,
    Source,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.pipeline.converters import (
    UnsupportedDocument,
    convert_document,
)
from knowledge_index.pipeline.distill import (
    build_profile_text,
    context_header,
    contextualize,
)
from knowledge_index.pipeline.matter_profile import mark_matter_dirty
from knowledge_index.pipeline.extraction import (
    AREA_OF_LAW_INSTRUCTION,
    MATTER_KIND_INSTRUCTION,
    CLASSIFY_SYSTEM,
    DECISION_SYSTEM,
    FILE_RELATION_SYSTEM,
    METADATA_SYSTEM,
    DecisionExtraction,
    DocumentMetadata,
    ExtractedParty,
    FileRelationResult,
    MatterClassification,
)
from knowledge_index.pipeline.folder_context import (
    build_member_doc,
    folder_ls,
)
from knowledge_index.entity_names import normalize_entity_name
from knowledge_index.pipeline.matter_search import (
    classification_tools,
    entity_search_covered,
    party_resolution_tools,
    relation_tools,
    resolve_or_create_entity,
)
from knowledge_index.pipeline.providers import (
    ModelOutputInvalid,
    chat_agent,
    chat_json,
    embed_text,
    usage_stage,
)
from knowledge_index.sync import (
    LocalFilesystemSource,
    PluginDropSource,
    SyncSource,
)
from knowledge_index.pipeline.ontology_tools import (
    clause_search_tool,
    ontology_navigation_tools,
    service_navigation_tools,
)
from knowledge_index.taxonomies import (
    ACCESS_ONLY_REINDEX,
    DISABLED_BY_CONFIGURATION,
    PIPELINE_STAGE_ORDER,
    PartyRole,
    WAITING_FOR_PREVIOUS_STAGE,
    PipelineStage,
    ProcessingStatus,
    RationaleCategory,
    RelationKind,
    VersionStatus,
)

ConnectorFactory = Callable[[Source, Session], SyncSource]

log = logging.getLogger(__name__)

# Stages the relate agent may pull forward for a neighbour it needs to read. Only
# model-free, straight-line stages qualify: they never open files, call tools, or
# wait on other documents, so inline execution cannot create a wait cycle.
_INLINE_READY_STAGES = (PipelineStage.FETCH.value, PipelineStage.CONVERT.value)

# Process-wide gate for inline conversions, sized from config on first use. Hatchet
# creates a PipelineRunner per task, so an instance attribute would not bound anything.
_inline_gate_lock = threading.Lock()
_inline_gate: threading.BoundedSemaphore | None = None


def _inline_conversion_gate(limit: int) -> threading.BoundedSemaphore:
    global _inline_gate
    with _inline_gate_lock:
        if _inline_gate is None:
            _inline_gate = threading.BoundedSemaphore(limit)
        return _inline_gate


@dataclass
class PipelineRun:
    processed: int = 0
    done: int = 0
    skipped: int = 0
    retried: int = 0
    quarantined: int = 0


@dataclass(frozen=True)
class StageResult:
    skip_reason: str | None = None


class RetryableStageError(RuntimeError):
    pass


def _typed_clauses(clause_scope, clauses) -> list[dict]:
    """Clause payloads with their model-chosen, validator-checked type nodes."""
    payloads: list[dict] = []
    for clause in clauses:
        entry = clause.model_dump()
        node = clause.clause_type_node if clause_scope else None
        entry["clause_type"] = node
        entry["clause_type_label"] = clause_scope.label_of(node) if node else None
        payloads.append(entry)
    return payloads


def _stage_trace(source_object_id: str, stage: str) -> tuple[str, list[str]]:
    """One never-recurring trace id per stage ATTEMPT, plus the gateway tags.

    doc:/stage: tags repeat by design (they group a file's history and a step's
    calls); trace: is a fresh uuid4 every attempt, so re-ingesting or re-typing
    the same document can never collide. The trace id is also written into the
    result's provenance, linking each DB row to its exact gateway trace."""
    trace_id = uuid4().hex
    return trace_id, [f"doc:{source_object_id}", f"stage:{stage}", f"trace:{trace_id}"]


class PipelineRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: AppConfig,
        *,
        connector_factory: ConnectorFactory | None = None,
        artifact_store: LocalArtifactStore | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.connector_factory = connector_factory or connector_from_source
        # One connector per source, not one per document: each carries an event loop,
        # a thread and an HTTP connection pool, and building one per fetch exhausts all
        # three on a corpus of any size.
        self._connectors: dict[str, SyncSource] = {}
        self.artifact_store = artifact_store or LocalArtifactStore(config.artifact_dir)

    def run_until_idle(self, *, limit: int | None = None) -> PipelineRun:
        result = PipelineRun()
        self.requeue_outdated_stages()
        self.requeue_newly_enabled_stages()
        self.recover_stale_claims()
        while limit is None or result.processed < limit:
            state_id = self._claim_next()
            if state_id is None:
                break
            outcome = self._execute_claim(state_id)
            result.processed += 1
            setattr(result, outcome, getattr(result, outcome) + 1)
        return result

    def run_stage_until_idle(
        self,
        stage: str,
        *,
        limit: int | None = None,
        prepare: bool = False,
    ) -> PipelineRun:
        """Drain one named stage so an external orchestrator can own the DAG."""
        if stage not in {item.value for item in PIPELINE_STAGE_ORDER}:
            raise ValueError(f"unknown pipeline stage: {stage}")
        result = PipelineRun()
        if prepare:
            self.requeue_outdated_stages()
            self.requeue_newly_enabled_stages()
            self.recover_stale_claims()
        while limit is None or result.processed < limit:
            state_id = self._claim_next(stage=stage)
            if state_id is None:
                break
            outcome = self._execute_claim(state_id)
            result.processed += 1
            setattr(result, outcome, getattr(result, outcome) + 1)
        return result

    def run_stage_for_object(self, stage: str, source_object_id: str) -> PipelineRun:
        """Run at most one stage claim for one source object.

        Hatchet uses this as the idempotency boundary for document-level workflows.
        A replay sees the durable state left by the previous attempt and simply skips
        stages that are already done, so changing orchestration never invalidates
        conversion artifacts or other completed work.
        """
        if stage not in {item.value for item in PIPELINE_STAGE_ORDER}:
            raise ValueError(f"unknown pipeline stage: {stage}")
        result = PipelineRun()
        state_id = self._claim_next(stage=stage, source_object_id=source_object_id)
        if state_id is None:
            return result
        outcome = self._execute_claim(state_id)
        result.processed = 1
        setattr(result, outcome, getattr(result, outcome) + 1)
        return result

    def requeue_outdated_stages(self) -> int:
        """Re-run a changed producer and invalidate only its downstream stages."""
        stage_names = [stage.value for stage in PIPELINE_STAGE_ORDER]
        with self.session_factory() as session:
            object_ids = session.scalars(select(ProcessingState.source_object_id).distinct()).all()
            changed = 0
            for object_id in object_ids:
                rows = {
                    row.stage: row
                    for row in session.scalars(
                        select(ProcessingState).where(ProcessingState.source_object_id == object_id)
                    ).all()
                }
                for index, stage_name in enumerate(stage_names):
                    row = rows.get(stage_name)
                    desired = self.config.pipeline.stage(stage_name)
                    if (
                        row is None
                        or not desired.enabled
                        or row.producer_version is None
                        or row.producer_version == desired.producer_version
                        or row.status
                        not in {ProcessingStatus.DONE.value, ProcessingStatus.SKIPPED.value}
                    ):
                        continue
                    self._requeue_stage_and_downstream(rows, stage_names, index)
                    changed += 1
                    break
            session.commit()
            return changed

    @staticmethod
    def _requeue_stage_and_downstream(
        rows: dict[str, ProcessingState], stage_names: list[str], index: int
    ) -> list[str]:
        """Queue one stage again and park every stage after it behind it.

        A stage's output is the next stage's input, so re-running one without invalidating
        what was derived from its old output leaves the document half-old — chunks indexed
        from a conversion that no longer exists, metadata extracted from a matter the file
        was since reclassified out of. Shared by the producer-version bump and the
        quarantine retry so an operator's re-run and a config re-run cannot drift apart.

        ``attempts`` goes back to zero, which hands the stage a fresh ``max_attempts``
        budget rather than bypassing it: a file that keeps failing quarantines again.
        """
        row = rows[stage_names[index]]
        row.status = ProcessingStatus.PENDING.value
        row.attempts = 0
        row.next_retry_at = None
        row.claimed_at = None
        row.last_error = None
        row.producer_version = None
        invalidated: list[str] = []
        for downstream_name in stage_names[index + 1 :]:
            downstream = rows.get(downstream_name)
            if downstream is None:
                continue
            downstream.status = ProcessingStatus.SKIPPED.value
            downstream.attempts = 0
            downstream.next_retry_at = None
            downstream.claimed_at = None
            downstream.last_error = {"reason": WAITING_FOR_PREVIOUS_STAGE}
            downstream.producer_version = None
            invalidated.append(downstream_name)
        return invalidated

    def retry_quarantined(self, source_object_id: str, *, stage: str | None = None) -> dict | None:
        """Release one quarantined document back into the pipeline.

        Quarantine is otherwise terminal: ``requeue_outdated_stages`` only reclaims rows
        that are done or skipped, so a file that failed once because a model was briefly
        unreachable would never be tried again. Returns ``None`` when the object has no
        quarantined stage, so the caller can 404 rather than report a retry that was not
        one.
        """
        stage_names = [item.value for item in PIPELINE_STAGE_ORDER]
        if stage is not None and stage not in stage_names:
            raise ValueError(f"unknown pipeline stage: {stage}")
        with self.session_factory() as session:
            rows = {
                row.stage: row
                for row in session.scalars(
                    select(ProcessingState).where(
                        ProcessingState.source_object_id == source_object_id
                    )
                ).all()
            }
            # The earliest quarantined stage, not the requested one blindly: everything
            # after it is invalidated anyway, so starting later would re-run downstream
            # work on an input that is still missing.
            candidates = [
                index
                for index, name in enumerate(stage_names)
                if (row := rows.get(name)) is not None
                and row.status == ProcessingStatus.QUARANTINED.value
                and (stage is None or name == stage)
            ]
            if not candidates:
                return None
            index = candidates[0]
            failure = rows[stage_names[index]].last_error or {}
            invalidated = self._requeue_stage_and_downstream(rows, stage_names, index)
            session.commit()
            return {
                "source_object_id": source_object_id,
                "stage": stage_names[index],
                "invalidated_stages": invalidated,
                "max_attempts": self.config.pipeline.stage(stage_names[index]).max_attempts,
                # A deterministic failure (an unreadable file, an oversized blob) will
                # quarantine again on the first attempt. Say so instead of letting the
                # operator read a queued row as a fix.
                "deterministic": bool(failure.get("deterministic")),
                "previous_error": {
                    key: value for key, value in failure.items() if key != "trace"
                },
            }

    def requeue_newly_enabled_stages(self) -> int:
        """Make an admin toggle effective without disturbing unrelated completed work."""
        with self.session_factory() as session:
            rows = session.scalars(
                select(ProcessingState).where(
                    ProcessingState.status == ProcessingStatus.SKIPPED.value
                )
            ).all()
            changed = 0
            for row in rows:
                if (row.last_error or {}).get("reason") != DISABLED_BY_CONFIGURATION:
                    continue
                if not self.config.pipeline.stage(row.stage).enabled:
                    continue
                row.status = ProcessingStatus.PENDING.value
                row.attempts = 0
                row.last_error = None
                row.next_retry_at = None
                changed += 1
            session.commit()
            return changed

    def requeue_ontology_outdated(self) -> int:
        """Selectively re-type documents whose node fell out of the active scope.

        Called after an ontology artifact or scope change. Documents whose type
        node is still visible keep their result; re-typed are (a) documents at a
        now-hidden node and (b) documents honestly left UNTYPED under a
        different scope — a richer artifact may finally have a home for them."""
        scope = self.config.doc_ontology()
        stage_names = [stage.value for stage in PIPELINE_STAGE_ORDER]
        metadata_index = stage_names.index(PipelineStage.EXTRACT_METADATA.value)
        with self.session_factory() as session:
            stale_documents = session.scalars(
                select(Document.id).where(
                    or_(
                        and_(
                            Document.doc_type.isnot(None),
                            Document.doc_type.notin_(sorted(scope.visible) or [""]),
                        ),
                        and_(
                            Document.doc_type.is_(None),
                            Document.ontology_fingerprint.isnot(None),
                            Document.ontology_fingerprint != scope.fingerprint,
                        ),
                    )
                )
            ).all()
            if not stale_documents:
                return 0
            object_ids = set(
                session.scalars(
                    select(DocumentVersionSource.source_object_id)
                    .join(
                        DocumentVersion,
                        DocumentVersion.id == DocumentVersionSource.version_id,
                    )
                    .where(DocumentVersion.document_id.in_(stale_documents))
                ).all()
            )
            changed = 0
            for object_id in sorted(object_ids):
                rows = {
                    row.stage: row
                    for row in session.scalars(
                        select(ProcessingState).where(
                            ProcessingState.source_object_id == object_id
                        )
                    ).all()
                }
                metadata_row = rows.get(PipelineStage.EXTRACT_METADATA.value)
                if metadata_row is None or metadata_row.status not in {
                    ProcessingStatus.DONE.value,
                    ProcessingStatus.SKIPPED.value,
                }:
                    continue
                metadata_row.status = ProcessingStatus.PENDING.value
                metadata_row.attempts = 0
                metadata_row.next_retry_at = None
                metadata_row.claimed_at = None
                metadata_row.last_error = None
                metadata_row.producer_version = None
                for downstream_name in stage_names[metadata_index + 1 :]:
                    downstream = rows.get(downstream_name)
                    if downstream is None:
                        continue
                    downstream.status = ProcessingStatus.SKIPPED.value
                    downstream.attempts = 0
                    downstream.next_retry_at = None
                    downstream.claimed_at = None
                    downstream.last_error = {"reason": "waiting_for_previous_stage"}
                    downstream.producer_version = None
                changed += 1
            session.commit()
            return changed

    def recover_stale_claims(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=self.config.pipeline.claim_timeout_seconds)
        with self.session_factory() as session:
            rows = session.scalars(
                select(ProcessingState).where(
                    ProcessingState.status == ProcessingStatus.RUNNING.value,
                    ProcessingState.claimed_at < cutoff,
                )
            ).all()
            for row in rows:
                row.status = ProcessingStatus.FAILED.value
                row.next_retry_at = datetime.now(UTC)
                row.last_error = {"class": "StaleClaim", "message": "worker claim expired"}
            session.commit()
            return len(rows)

    def _claim_next(
        self, *, stage: str | None = None, source_object_id: str | None = None
    ) -> str | None:
        now = datetime.now(UTC)
        eligible = or_(
            ProcessingState.status == ProcessingStatus.PENDING.value,
            and_(
                ProcessingState.status == ProcessingStatus.FAILED.value,
                or_(
                    ProcessingState.next_retry_at.is_(None),
                    ProcessingState.next_retry_at <= now,
                ),
            ),
        )
        with self.session_factory() as session:
            statement = (
                select(ProcessingState)
                .where(eligible)
                .order_by(ProcessingState.updated_at, ProcessingState.id)
                .limit(1)
            )
            if stage is not None:
                statement = statement.where(ProcessingState.stage == stage)
            if source_object_id is not None:
                statement = statement.where(
                    ProcessingState.source_object_id == source_object_id
                )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            state = session.scalar(statement)
            if state is None:
                return None
            access_only_marker = (
                dict(state.last_error)
                if state.stage == PipelineStage.INDEX.value
                and (state.last_error or {}).get("reason") == ACCESS_ONLY_REINDEX
                else None
            )
            state.status = ProcessingStatus.RUNNING.value
            state.claimed_at = now
            state.attempts += 1
            state.next_retry_at = None
            state.last_error = access_only_marker
            state_id = state.id
            session.commit()
            return state_id

    def _execute_claim(self, state_id: str) -> str:
        with self.session_factory() as session:
            state = session.get(ProcessingState, state_id)
            if state is None or state.status != ProcessingStatus.RUNNING.value:
                return "retried"
            stage_config = self.config.pipeline.stage(state.stage)
            try:
                if not stage_config.enabled:
                    stage_result = StageResult(DISABLED_BY_CONFIGURATION)
                else:
                    # One claim is one stage's work: everything the handler and its tool
                    # loops spend on the gateway is booked against this stage name.
                    with usage_stage(state.stage):
                        stage_result = self._dispatch(session, state)
                state.status = (
                    ProcessingStatus.SKIPPED.value
                    if stage_result.skip_reason
                    else ProcessingStatus.DONE.value
                )
                state.last_error = (
                    {"reason": stage_result.skip_reason} if stage_result.skip_reason else None
                )
                state.producer_version = stage_config.producer_version
                state.claimed_at = None
                self._unlock_next(session, state)
                session.commit()
                return "skipped" if stage_result.skip_reason else "done"
            except ModelOutputInvalid as exc:
                session.rollback()
                state = session.get(ProcessingState, state_id)
                if state is None:
                    return "retried"
                return self._record_failure(session, state, exc, deterministic=False)
            except (UnsupportedDocument, ArtifactTooLarge, FileNotFoundError, ValueError) as exc:
                session.rollback()
                state = session.get(ProcessingState, state_id)
                if state is None:
                    return "quarantined"
                return self._record_failure(session, state, exc, deterministic=True)
            except Exception as exc:  # a worker failure must never stop the corpus
                session.rollback()
                state = session.get(ProcessingState, state_id)
                if state is None:
                    return "retried"
                return self._record_failure(session, state, exc, deterministic=False)

    def _record_failure(
        self,
        session: Session,
        state: ProcessingState,
        error: Exception,
        *,
        deterministic: bool,
    ) -> str:
        # A manually-expired task's zombie attempt can outlive its claim: the
        # re-dispatched attempt completes the stage, then the zombie fails and
        # would stamp its failure over the finished work (2026-08-01 run: a
        # done classify flagged quarantined). A failure may only be recorded
        # over a claim this attempt still plausibly owns — never over DONE.
        current_status = session.scalar(
            select(ProcessingState.status).where(ProcessingState.id == state.id)
        )
        if current_status == ProcessingStatus.DONE.value:
            return "superseded"
        stage_config = self.config.pipeline.stage(state.stage)
        quarantine = deterministic or state.attempts >= stage_config.max_attempts
        state.status = (
            ProcessingStatus.QUARANTINED.value if quarantine else ProcessingStatus.FAILED.value
        )
        state.claimed_at = None
        state.last_error = {
            "class": type(error).__name__,
            "message": str(error)[:2000],
            "trace": "".join(traceback.format_exception(error))[-6000:],
            "deterministic": deterministic,
        }
        if not quarantine:
            delay = self.config.pipeline.retry_base_seconds * (2 ** (state.attempts - 1))
            state.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        session.commit()
        return "quarantined" if quarantine else "retried"

    def _dispatch(self, session: Session, state: ProcessingState) -> StageResult:
        handlers = {
            PipelineStage.FETCH.value: self._fetch,
            PipelineStage.CONVERT.value: self._convert,
            PipelineStage.CLASSIFY_MATTER.value: self._classify_matter,
            PipelineStage.RELATE.value: self._relate,
            PipelineStage.EXTRACT_METADATA.value: self._extract_metadata,
            PipelineStage.EXTRACT_DECISIONS.value: self._extract_decisions,
            PipelineStage.GEN_EVALS.value: self._generate_evals,
            PipelineStage.INDEX.value: self._index,
        }
        return handlers[state.stage](session, state)

    def _connector_for(self, source: Source, session: Session) -> SyncSource:
        connector = self._connectors.get(source.id)
        if connector is None:
            connector = self.connector_factory(source, session)
            self._connectors[source.id] = connector
        return connector

    @staticmethod
    def _open_content(connector: SyncSource, source_object: SourceObject):
        """Open one object's bytes.

        Connectors that staged content during the scan recorded where; opening that path
        is a local read in any process. Only sources that can cheaply locate an object by
        id (a mounted folder, a plugin drop directory) are asked to fetch it — for an API
        source that would mean re-crawling the estate.
        """
        opener = getattr(connector, "open_staged", None)
        if opener is not None:
            return opener(source_object.staged_path, source_object.external_id)
        return connector.fetch(source_object.external_id)

    def close(self) -> None:
        """Release connector resources (event loops, threads, HTTP pools)."""
        _close_connectors(self._connectors)

    def __enter__(self) -> "PipelineRunner":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _fetch(self, session: Session, state: ProcessingState) -> StageResult:
        source_object = session.get(SourceObject, state.source_object_id)
        if source_object is None or source_object.deleted_at is not None:
            return StageResult("source_object_deleted")
        source = session.get(Source, source_object.source_id)
        if source is None:
            raise ValueError("source record does not exist")
        connector = self._connector_for(source, session)
        with self._open_content(connector, source_object) as stream:
            stored = self.artifact_store.put_blob(
                stream,
                max_bytes=self.config.pipeline.max_file_mb * 1024 * 1024,
            )
        blob = session.get(Blob, stored.content_hash)
        if blob is None:
            blob = Blob(
                content_hash=stored.content_hash,
                size_bytes=stored.size_bytes,
                mime_sniffed=source_object.mime_type,
                cached_path=str(stored.path),
            )
            session.add(blob)
        elif not blob.cached_path:
            blob.cached_path = str(stored.path)
        source_object.content_hash = stored.content_hash
        return StageResult()

    def _convert(self, session: Session, state: ProcessingState) -> StageResult:
        source_object = _source_object_with_hash(session, state)
        existing = _artifact(session, source_object.content_hash, "structured_json")
        if existing is not None:
            return StageResult()
        blob = session.get(Blob, source_object.content_hash)
        if blob is None or not blob.cached_path:
            raise RetryableStageError("fetched blob is not available in the artifact store")
        converted = convert_document(
            Path(blob.cached_path),
            name=source_object.name,
            mime_type=source_object.mime_type,
            config=self.config,
        )
        session.add(
            Artifact(
                content_hash=source_object.content_hash,
                producer="docling-serve",
                producer_version=self.config.pipeline.stage("convert").producer_version,
                kind="structured_json",
                payload=converted.as_payload(),
            )
        )
        return StageResult()

    def _classify_matter(self, session: Session, state: ProcessingState) -> StageResult:
        source_object = _source_object_with_hash(session, state)
        source = session.get(Source, source_object.source_id)
        if source is None:
            raise RetryableStageError("source record is missing")
        converted = _required_artifact(session, source_object.content_hash, "structured_json")
        text = (converted.payload or {}).get("text", "")
        folder = _parent_folder(source_object.path)
        model = self.config.pipeline.stage("classify_matter").model
        trace_id, trace_tags = _stage_trace(source_object.id, "classify_matter")
        # Area of Law is a shallow facet: a compact menu in the prompt beats an
        # agentic walk. Only offered (and validated) when the facet is active.
        area_scope = None
        classify_system = CLASSIFY_SYSTEM
        if "area_of_law" in self.config.ontology.active_facets:
            area_scope = self.config.ontology_facet("area_of_law")
            classify_system = (
                classify_system + AREA_OF_LAW_INSTRUCTION + area_scope.indented_menu()
            )
        # matter_kind: the Service facet is deep (328 nodes, depth 6) and its
        # labels mislead ("Services Agreement Practice" is not what it sounds
        # like) — the agent walks it with tools and judges by DEFINITIONS, under
        # the same visited-id discipline as document typing.
        service_scope = None
        service_visited: set[str] = set()
        if "service" in self.config.ontology.active_facets:
            service_scope = self.config.ontology_facet("service")
            classify_system = classify_system + MATTER_KIND_INSTRUCTION
        # The fallback bucket is keyed to the file's OWN folder, not the top-level
        # folder: strays of one folder likely belong together (the folder is the
        # firm's own filing statement), but strays of different clients/cases must
        # never converge into one shared matter (audit §4.3 — 25 files of many
        # clients ended up in one "UNASSIGNED-CLIENTS" matter).
        fallback_reference = f"UNASSIGNED-{_slug(folder) or 'ROOT'}"
        seen_matter_ids: set[str] = set()

        def validate_classification(candidate: MatterClassification) -> str | None:
            if candidate.matter_id is not None and candidate.matter_id not in seen_matter_ids:
                return (
                    f"matter_id {candidate.matter_id!r} did not appear in any tool result; "
                    "copy the id from a search_matters, peek_matter, or create_matter "
                    "result, or null"
                )
            if candidate.practice_area_node is not None and area_scope is not None:
                if area_scope.resolve(candidate.practice_area_node) is None:
                    return (
                        f"practice_area_node {candidate.practice_area_node!r} is not in "
                        "the area-of-law menu; pick an id from the menu or null"
                    )
            if candidate.matter_kind_node is not None and service_scope is not None:
                if candidate.matter_kind_node not in service_visited:
                    return (
                        f"matter_kind_node {candidate.matter_kind_node!r} did not appear "
                        "in any service_* tool result; look the service up and submit an "
                        "id you have seen, or null"
                    )
                if candidate.matter_kind_node not in service_scope.visible:
                    return (
                        f"matter_kind_node {candidate.matter_kind_node!r} is not part of "
                        "the active service facet"
                    )
            return None
        # Agentic classification: a firm has hundreds–thousands of matters, so instead of
        # dumping a truncated list into the prompt, the model SEARCHES existing matters and
        # inspects the ±2-level folder neighbourhood before deciding.
        classification = chat_agent(
            model,
            self.config,
            system=classify_system,
            user=json.dumps(
                {
                    "path": source_object.path,
                    "filename": source_object.name,
                    "folder": folder,
                    "folder_neighbourhood": folder_ls(
                        session, source_object.source_id, source_object.path
                    ),
                    "text": text[:8000],
                },
                ensure_ascii=False,
            ),
            tools=classification_tools(
                session,
                self.config,
                source_object.source_id,
                source_object.path,
                # create_matter commits in its own session so the matter is visible to
                # concurrently classifying documents the moment the tool returns.
                session_factory=self.session_factory,
                project_id=source.project_id,
                fallback_reference=fallback_reference,
                provenance={
                    "model": model,
                    "prompt_version": self.config.pipeline.stage(
                        "classify_matter"
                    ).producer_version,
                    "evidence": [source_object.path],
                    "trace_id": trace_id,
                },
                seen_matter_ids=seen_matter_ids,
            )
            + (
                service_navigation_tools(service_scope, service_visited)
                if service_scope is not None
                else []
            ),
            final_schema=MatterClassification,
            # Nothing here is capped: a truncated service listing hides the right
            # candidate, and an exhausted turn budget makes the agent give up
            # mid-investigation — both produce confident wrong metadata.
            trace_tags=trace_tags,
            result_validator=validate_classification,
        )
        practice_area = (
            area_scope.resolve(classification.practice_area_node)
            if area_scope is not None
            else None
        )
        matter_kind = (
            service_scope.resolve(classification.matter_kind_node)
            if service_scope is not None
            else None
        )
        matter_ref = (classification.matter_ref or "").strip().upper() or fallback_reference
        provenance = {
            "model": model,
            "prompt_version": self.config.pipeline.stage("classify_matter").producer_version,
            "confidence": classification.confidence,
            "evidence": [source_object.path, classification.reasoning],
            "trace_id": trace_id,
        }
        if practice_area and area_scope is not None:
            provenance["area_fingerprint"] = area_scope.fingerprint
        # The agent names the matter it chose (found via search or created with its
        # create_matter tool) by id; the ref-based lock-scan-create below stays as the
        # fallback for a null or dangling id.
        matter = session.get(Matter, classification.matter_id) if classification.matter_id else None
        if matter is not None and source.project_id and matter.project_id != source.project_id:
            matter = None
        if matter is None:
            _advisory_xact_lock(
                session,
                f"matter-ref:{source.project_id or 'none'}:{matter_ref}",
            )
            matter = next(
                (
                    item
                    for item in session.scalars(select(Matter)).all()
                    if matter_ref in (item.reference_numbers or [])
                    and (not source.project_id or item.project_id == source.project_id)
                ),
                None,
            )
        if matter is None:
            project = session.get(Project, source.project_id) if source.project_id else None
            if source.project_id and project is None:
                raise RetryableStageError("source's assigned project is missing")
            # A fallback holding matter announces itself: named after its folder
            # (never after whichever document arrived first — the audit's bucket
            # called itself after a random member and read as a real matter) and
            # status "unassigned" so the UI can surface it as a triage pile.
            unassigned = matter_ref == fallback_reference
            matter = Matter(
                project_id=project.id if project else None,
                reference_numbers=[matter_ref],
                title=(
                    f"Unassigned — {folder or '/'}"
                    if unassigned
                    else classification.matter_title or matter_ref
                ),
                # Both labels are left unset here and derived from the vote below,
                # so the document that happens to create the matter carries exactly
                # the same weight as the twenty that join it.
                status="unassigned" if unassigned else "unknown",
                imported=False,
                provenance=provenance,
            )
            session.add(matter)
            session.flush()
        if not matter.imported:
            # A matter's area and kind are properties of the MATTER, but this agent
            # only ever sees ONE document, so its answer is about that document.
            # "First valid answer wins" then froze whichever parallel call returned
            # first: a Master Clinical Trial Agreement became Contract Law (a CTA is
            # a contract), a continuation-vehicle LPA became Tax Law (its §1061 memo
            # won the race), a co-invest LP became M&A (the deal it funded). Each
            # answer was right about its document and wrong about the matter.
            #
            # Every document votes instead, weighted by the agent's own confidence,
            # and the running mode is the label. Order stops mattering, so the
            # anti-flapping property that first-wins was protecting survives — a
            # settled matter only changes when the evidence does. Practice area and
            # matter kind go through the identical path: they were both first-wins,
            # and a matter that asserts "Funds Practice" and "Tax Law" at once is
            # exactly what two independent races produce.
            _record_matter_vote(
                matter,
                area=practice_area,
                kind=matter_kind,
                weight=float(classification.confidence or 0.0),
            )
        # The matter's own facts — practice, service, lifecycle, what the deal is,
        # which files are versions of one another — cannot be seen from here, and
        # the vote above only makes this document's guess stable, not right. Record
        # that the matter changed; a matter-level pass re-derives them from the
        # folder once its documents settle. One upsert, no new task.
        if not matter.imported:
            mark_matter_dirty(session, matter.id)
        if matter.project_id is None and source.project_id:
            project = session.get(Project, source.project_id)
            if project is None:
                raise RetryableStageError("source's assigned project is missing")
            matter.project_id = project.id
        if _artifact(session, source_object.content_hash, "classification") is None:
            session.add(
                Artifact(
                    content_hash=source_object.content_hash,
                    producer="classify-llm",
                    producer_version=self.config.pipeline.stage(
                        "classify_matter"
                    ).producer_version,
                    kind="classification",
                    payload=classification.model_dump(),
                )
            )
        assignment = session.get(MatterAssignment, source_object.id)
        if assignment is None:
            session.add(
                MatterAssignment(
                    source_object_id=source_object.id,
                    matter_id=matter.id,
                    confidence=classification.confidence,
                    evidence=[source_object.path, classification.reasoning],
                    producer_version=self.config.pipeline.stage("classify_matter").producer_version,
                )
            )
        else:
            assignment.matter_id = matter.id
            assignment.confidence = classification.confidence
            assignment.evidence = [source_object.path, classification.reasoning]
            assignment.producer_version = self.config.pipeline.stage(
                "classify_matter"
            ).producer_version
        # Relations other files decided while this one was still unclassified were
        # parked; the assignment they were waiting for lands with this transaction.
        self._apply_relation_intents(session, source_object.id)
        return StageResult()

    def _relate(self, session: Session, state: ProcessingState) -> StageResult:
        """Relate one arriving file using its local filing context and selective reads."""
        state_id = state.id
        source_object = _source_object_with_hash(session, state)
        source_object_id = source_object.id
        content_hash = source_object.content_hash
        assignment = session.get(MatterAssignment, source_object.id)
        if assignment is None:
            raise RetryableStageError("matter assignment is missing")
        matter = session.get(Matter, assignment.matter_id)
        if matter is None:
            raise RetryableStageError("assigned matter is missing")

        matter_id = matter.id
        source_id = source_object.source_id
        producer_version = self.config.pipeline.stage("relate").producer_version
        member = build_member_doc(session, source_object)
        matter_reference_numbers = list(matter.reference_numbers or [])
        matter_title = matter.title
        folder_neighbourhood = folder_ls(
            session,
            source_id,
            source_object.path,
            up=1,
            down=1,
            per_folder_limit=None,
            max_folders=None,
        )

        # The prompt snapshot is now fully detached. No DB transaction or advisory lock
        # remains open while the model calls open_file/search tools.
        trace_id, trace_tags = _stage_trace(source_object_id, "relate")
        session.rollback()
        result = self._relate_file(
            matter_reference_numbers=matter_reference_numbers,
            matter_title=matter_title,
            member=member,
            source_id=source_id,
            folder_neighbourhood=folder_neighbourhood,
            trace_tags=trace_tags,
        )

        # Lock only the files whose knowledge rows may be written. Sorting prevents a pair
        # of concurrent files that reference each other from taking locks in opposite order.
        target_refs = _file_relation_target_refs(result)
        for ref in sorted({source_object_id, *target_refs}):
            _advisory_xact_lock(session, f"relate-file:{ref}")
        session.expire_all()

        state = session.get(ProcessingState, state_id)
        if state is None or state.status != ProcessingStatus.RUNNING.value:
            raise RetryableStageError("relation claim changed while inference was running")
        source_object = _source_object_with_hash(session, state)
        if source_object.content_hash != content_hash:
            raise RetryableStageError("file content changed during relation inference")
        assignment = session.get(MatterAssignment, source_object_id)
        if assignment is None:
            raise RetryableStageError("matter assignment disappeared during relation inference")
        if assignment.matter_id != matter_id:
            raise RetryableStageError("matter assignment changed during relation inference")
        matter = session.get(Matter, matter_id)
        if matter is None:
            raise RetryableStageError("assigned matter disappeared during relation inference")
        self._materialize_file_relation(
            session,
            matter,
            source_object,
            result,
            producer_version,
            trace_id=trace_id,
        )
        return StageResult()

    def _relate_file(
        self,
        *,
        matter_reference_numbers: list,
        matter_title: str,
        member,
        source_id: str,
        folder_neighbourhood: str,
        trace_tags: list[str] | None = None,
    ) -> FileRelationResult:
        model = self.config.pipeline.stage("relate").model
        opened_refs: set[str] = set()

        def validate_opened_refs(candidate: FileRelationResult) -> str | None:
            unopened_refs = _file_relation_target_refs(candidate) - opened_refs
            if not unopened_refs:
                return None
            return (
                "these target refs were not inspected with open_file: "
                + ", ".join(sorted(unopened_refs))
            )

        result = chat_agent(
            model,
            self.config,
            system=FILE_RELATION_SYSTEM,
            user=json.dumps(
                {
                    "matter": {
                        "reference_numbers": matter_reference_numbers,
                        "title": matter_title,
                    },
                    "current_path": member.path,
                    "current_file": member.as_prompt(text_chars=len(member.text)),
                    "folder_listing_one_level_up_and_down": folder_neighbourhood,
                },
                ensure_ascii=False,
            ),
            tools=relation_tools(
                self.session_factory,
                self.config,
                source_id,
                member.path,
                opened_refs=opened_refs,
                ensure_ready=self.ensure_source_object_ready,
            ),
            final_schema=FileRelationResult,
            result_validator=validate_opened_refs,
            trace_tags=trace_tags,
        )
        return result

    def _materialize_file_relation(
        self,
        session: Session,
        matter: Matter,
        source_object: SourceObject,
        result: FileRelationResult,
        producer_version: str,
        trace_id: str | None = None,
    ) -> None:
        provenance = {
            "model": self.config.pipeline.stage("relate").model,
            "prompt_version": producer_version,
            "method": "file-scoped-ai-relation",
            "confidence": result.confidence,
            "source_object_id": source_object.id,
            "trace_id": trace_id,
        }
        # A re-relate replaces this file's undelivered decisions: whatever is still
        # pending from a previous run is superseded by the result being applied now.
        session.execute(
            delete(RelationIntent).where(
                RelationIntent.source_object_id == source_object.id,
                RelationIntent.status == "pending",
            )
        )
        current_document, current_version = self._ensure_file_entity(
            session, source_object, matter, provenance
        )
        # The entity ensured above is abandoned when the identity decision merges this
        # file into another document; _absorb_abandoned_entity re-points its edges to
        # the survivor and deletes the emptied rows at the end of this method.
        initial_document_id, initial_version_id = current_document.id, current_version.id

        identity_ref = result.duplicate_of or result.same_document_ref
        target_entity = self._file_entity_for_ref(session, identity_ref, provenance)
        if result.identity == "duplicate" and target_entity is not None:
            target_document, target_version = target_entity
            _relink_version_source(session, target_version.id, source_object.id)
            current_document, current_version = target_document, target_version
        elif result.identity == "new_version" and target_entity is not None:
            target_document, target_version = target_entity
            if (
                current_version.id == target_version.id
                or _version_source_count(session, current_version.id) > 1
            ):
                current_version = self._create_version(
                    session, target_document, source_object, provenance
                )
                _relink_version_source(session, current_version.id, source_object.id)
            else:
                current_version.document_id = target_document.id
            current_document = target_document
            self._order_file_version(
                session,
                current_document,
                current_version,
                target_version,
                result.relative_order,
                provenance,
            )
        elif not _document_is_exclusive_to_source(
            session, current_document.id, source_object.id
        ) and not _version_is_chain_anchor(
            session, current_document.id, current_version.id
        ):
            # identity=new_document while sharing a document with other files. Two very
            # different situations produce this: (a) THIS file previously declared
            # itself a version of another file's document (an identity flip on
            # re-relate) — it must be able to leave; (b) OTHER files declared
            # themselves versions of THIS file (live merge or replayed intent) — the
            # anchor honestly answering "I am not a version of anything I see" is NOT
            # a contradiction of their attachment, and splitting the anchor out would
            # undo the merge. Only the non-anchor case moves out.
            previous_document = current_document
            current_document, current_version = self._create_file_entity(
                session, source_object, matter, provenance
            )
            _relink_version_source(session, current_version.id, source_object.id)
            # The chain it just left has a hole where this version used to sit.
            _renumber_chain(session, previous_document)
        if (
            result.identity in {"duplicate", "new_version"}
            and identity_ref
            and identity_ref != source_object.id
            and target_entity is None
        ):
            # The model identified this file as a version/copy of a neighbour that has
            # no matter assignment yet (bulk ingest races arrival order). The file
            # stays a standalone document for now; the identity decision is parked and
            # replayed by the target's classify — the replay merges the two documents
            # and requeues this file's knowledge stages under the survivor.
            self._park_intent(
                session,
                source_object.id,
                identity_ref,
                intent="identity",
                relation_kind="",
                payload={
                    "identity": result.identity,
                    "relative_order": result.relative_order,
                },
                provenance=provenance,
            )

        # Typing is owned by extract_metadata (the ontology walk); relate only
        # establishes identity, versions, and relations. The logical title belongs
        # to the NEWEST version's view of the document — an older draft arriving
        # late must not rename the whole chain (last-arrival-wins churn).
        if not current_document.title or _version_is_newest(
            session, current_document.id, current_version
        ):
            current_document.title = result.logical_title or current_document.title
        current_document.matter_id = matter.id
        current_document.project_id = matter.project_id
        current_document.provenance = provenance
        if _is_email_file(session, source_object):
            # A sent email IS its final record — there is no draft of a received
            # email. The prompt states this, but models ignored it 78× in the audit
            # (§2.4), dropping those emails out of every only_final view. Emails are
            # the one status the code can decide deterministically, so it does.
            current_version.status = VersionStatus.FINAL.value
            current_version.status_evidence = {
                "related_from": source_object.id,
                "rule": "email_is_final",
            }
        else:
            current_version.status = (
                result.status
                if result.status in {item.value for item in VersionStatus}
                else VersionStatus.UNKNOWN.value
            )
            current_version.status_evidence = {"related_from": source_object.id}
        current_version.provenance = provenance

        redline_entity = self._file_entity_for_ref(session, result.redline_of, provenance)
        if redline_entity is not None and redline_entity[1].id != current_version.id:
            current_version.redline_against = redline_entity[1].id
        elif redline_entity is None and result.redline_of:
            self._park_intent(
                session,
                source_object.id,
                result.redline_of,
                intent="redline",
                relation_kind="",
                payload={},
                provenance=provenance,
            )
        # the reverse declaration: an opened neighbour is a markup OF the current file
        markup_entity = self._file_entity_for_ref(session, result.redline_by, provenance)
        if markup_entity is not None and markup_entity[1].id != current_version.id:
            markup_entity[1].redline_against = current_version.id
        elif markup_entity is None and result.redline_by:
            self._park_intent(
                session,
                source_object.id,
                result.redline_by,
                intent="redline_by",
                relation_kind="",
                payload={},
                provenance=provenance,
            )

        allowed = {
            RelationKind.ANNEX_OF.value,
            RelationKind.RESPONDS_TO.value,
            RelationKind.REFERENCES.value,
            RelationKind.AMENDS.value,
        }
        for relation in result.relations:
            if relation.kind not in allowed:
                continue
            target = self._file_entity_for_ref(session, relation.target_ref, provenance)
            if target is None:
                self._park_intent(
                    session,
                    source_object.id,
                    relation.target_ref,
                    intent="relation",
                    relation_kind=relation.kind,
                    payload={"direction": relation.direction, "evidence": relation.evidence},
                    provenance=provenance,
                )
                continue
            if target[0].id == current_document.id:
                continue
            from_document, to_document = (
                (current_document, target[0])
                if relation.direction == "current_to_target"
                else (target[0], current_document)
            )
            _ensure_relation(
                session,
                from_type="document",
                from_id=from_document.id,
                to_type="document",
                to_id=to_document.id,
                kind=relation.kind,
                provenance={**provenance, "evidence": [relation.evidence]},
            )

        self._assign_thread(
            session, matter, source_object, current_document, result, provenance
        )

        session.flush()
        self._absorb_abandoned_entity(
            session,
            ghost_document_id=initial_document_id,
            ghost_version_id=initial_version_id,
            survivor_document_id=current_document.id,
            survivor_version_id=current_version.id,
        )
        _refresh_latest_final(session, current_document)
        # relate can move a dated document into (or create it in) this matter — e.g.
        # a replayed identity merge folding a fully-processed file in — so the span
        # is re-derived here as well as where dates are born (extract_metadata).
        _refresh_matter_time_range(session, matter)

    def _absorb_abandoned_entity(
        self,
        session: Session,
        *,
        ghost_document_id: str,
        ghost_version_id: str,
        survivor_document_id: str,
        survivor_version_id: str,
    ) -> None:
        """Fold an entity the identity decision walked away from into its survivor.

        _ensure_file_entity creates a Document+Version before the identity branch;
        a duplicate/new_version outcome moves the content elsewhere and would leave
        those rows behind as zero-source/zero-version husks that listings and the
        graph still show. Everything that references the husk is re-pointed to the
        surviving entity (relations with dedupe, redline_against, decision records)
        or dropped where it is derived data the pipeline rebuilds (chunks, grants),
        then the emptied rows are deleted. Runs under the relate advisory locks, so
        no concurrent materialization can touch edges involving these entities."""
        version = (
            session.get(DocumentVersion, ghost_version_id)
            if ghost_version_id != survivor_version_id
            else None
        )
        if version is not None and _version_source_count(session, version.id) == 0:
            _repoint_relations(
                session, "document_version", ghost_version_id, survivor_version_id
            )
            for row in session.scalars(
                select(DocumentVersion).where(
                    DocumentVersion.redline_against == ghost_version_id
                )
            ):
                row.redline_against = (
                    survivor_version_id if row.id != survivor_version_id else None
                )
            for record in session.scalars(
                select(DecisionRecord).where(
                    or_(
                        DecisionRecord.version_from == ghost_version_id,
                        DecisionRecord.version_to == ghost_version_id,
                    )
                )
            ):
                if record.version_from == ghost_version_id:
                    record.version_from = survivor_version_id
                if record.version_to == ghost_version_id:
                    record.version_to = survivor_version_id
            ghost_chunk_ids = session.scalars(
                select(Chunk.id).where(Chunk.document_version_id == ghost_version_id)
            ).all()
            session.execute(
                delete(Chunk).where(Chunk.document_version_id == ghost_version_id)
            )
            if ghost_chunk_ids:
                # The ghost was already indexed (a replayed identity merge, or a
                # re-relate after the index stage ran). Postgres is authoritative and
                # just dropped the rows; sync the deletion to OpenSearch best-effort —
                # a search hiccup must not fail the merge, and an orphaned search doc
                # is invisible anyway once its chunk row is gone.
                try:
                    from knowledge_index.search_backend import OpenSearchIndex

                    OpenSearchIndex(self.config).bulk_sync(
                        deletes=ghost_chunk_ids, upserts=[]
                    )
                except Exception:
                    log.warning(
                        "could not sync %d ghost-chunk deletions to the search index",
                        len(ghost_chunk_ids),
                        exc_info=True,
                    )
            session.delete(version)
            session.flush()
        document = (
            session.get(Document, ghost_document_id)
            if ghost_document_id != survivor_document_id
            else None
        )
        if document is not None and not session.scalar(
            select(DocumentVersion.id)
            .where(DocumentVersion.document_id == ghost_document_id)
            .limit(1)
        ):
            _repoint_relations(
                session, "document", ghost_document_id, survivor_document_id
            )
            session.execute(
                delete(DocumentGrant).where(
                    DocumentGrant.document_id == ghost_document_id
                )
            )
            for record in session.scalars(
                select(DecisionRecord).where(
                    DecisionRecord.document_id == ghost_document_id
                )
            ):
                record.document_id = survivor_document_id
            session.delete(document)
            session.flush()

    def _assign_thread(
        self,
        session: Session,
        matter: Matter,
        source_object: SourceObject,
        current_document: Document,
        result: FileRelationResult,
        provenance: dict,
    ) -> None:
        """Thread the current file with layered evidence, strongest first.

        1. RFC 5322 Message-ID linkage (authoritative where headers exist).
        2. Code-normalized subject equality (survives Re:/Fwd: prefixes but never
           invents joins).
        3. The model's judgment: declared members join the thread, and any document
           that thereby belongs to two threads of one matter proves those threads
           are the same conversation — they merge. Two independent model calls thus
           unify a thread neither of them could name identically.
        Every email gets a thread (a lone email is a one-message thread), so
        participants and time_range exist corpus-wide and later replies can join.
        """
        email_ctx = _email_thread_context(session, source_object)
        if email_ctx is None and not result.thread_subject:
            return

        thread = None
        if email_ctx is not None and email_ctx["ids"]:
            linked = [
                candidate
                for candidate in session.scalars(
                    select(CommunicationThread).where(
                        CommunicationThread.matter_id == matter.id
                    )
                ).all()
                if set(candidate.message_ids or []) & email_ctx["ids"]
            ]
            if linked:
                thread = linked[0]
                # the current email bridges formerly separate fragments of one chain
                for ghost in linked[1:]:
                    _merge_threads(session, thread, ghost)
        subject_norm = _normalize_subject(
            (email_ctx or {}).get("subject") or result.thread_subject or ""
        )
        if thread is None and subject_norm:
            thread = session.scalar(
                select(CommunicationThread).where(
                    CommunicationThread.matter_id == matter.id,
                    CommunicationThread.subject_norm == subject_norm,
                )
            )
        if thread is None:
            thread = CommunicationThread(
                matter_id=matter.id, subject_norm=subject_norm or None
            )
            session.add(thread)
            session.flush()

        if email_ctx is not None:
            thread.message_ids = sorted(set(thread.message_ids or []) | email_ctx["ids"])
            merged = list(thread.participants or [])
            merged.extend(p for p in email_ctx["participants"] if p not in merged)
            thread.participants = merged
            thread.time_range = _merge_time_range(thread.time_range, email_ctx["date"])

        thread_documents = {current_document.id}
        for ref in result.thread_member_refs:
            entity = self._file_entity_for_ref(session, ref, provenance)
            if entity is not None:
                thread_documents.add(entity[0].id)
            else:
                self._park_intent(
                    session,
                    source_object.id,
                    ref,
                    intent="thread_member",
                    relation_kind="",
                    payload={
                        "thread_subject": thread.subject_norm,
                        "matter_id": matter.id,
                    },
                    provenance=provenance,
                )
        for document_id in thread_documents:
            _ensure_relation(
                session,
                from_type="document",
                from_id=document_id,
                to_type="thread",
                to_id=thread.id,
                kind=RelationKind.BELONGS_TO_THREAD.value,
                provenance=provenance,
            )
        session.flush()
        for document_id in thread_documents:
            self._unify_document_threads(session, matter, document_id, thread)

    def _unify_document_threads(
        self, session: Session, matter: Matter, document_id: str, survivor: CommunicationThread
    ) -> None:
        """A document in two threads of one matter proves the threads are one."""
        memberships = session.scalars(
            select(Relation).where(
                Relation.from_type == "document",
                Relation.from_id == document_id,
                Relation.to_type == "thread",
                Relation.kind == RelationKind.BELONGS_TO_THREAD.value,
            )
        ).all()
        for membership in memberships:
            if membership.to_id == survivor.id:
                continue
            other = session.get(CommunicationThread, membership.to_id)
            if other is None:
                session.delete(membership)
                continue
            if other.matter_id != matter.id:
                continue  # cross-matter membership stays a separate thread
            _merge_threads(session, survivor, other)

    def _park_intent(
        self,
        session: Session,
        origin_id: str,
        target_ref: str,
        *,
        intent: str,
        relation_kind: str,
        payload: dict,
        provenance: dict,
    ) -> None:
        """Persist a relate decision whose target has no matter assignment yet.

        Only parks against a target that genuinely exists and was readable (alive,
        fetched) — anything else is a dead ref with nothing to replay against. Runs
        under the relate advisory locks on {origin, target}, so it cannot race the
        replay in classify_matter: whichever transaction wins the lock sees the
        other's committed state.
        """
        target = session.get(SourceObject, target_ref)
        if target is None or target.deleted_at is not None or not target.content_hash:
            return
        existing = session.scalar(
            select(RelationIntent).where(
                RelationIntent.source_object_id == origin_id,
                RelationIntent.target_source_object_id == target_ref,
                RelationIntent.intent == intent,
                RelationIntent.relation_kind == relation_kind,
            )
        )
        if existing is not None:
            return
        session.add(
            RelationIntent(
                source_object_id=origin_id,
                target_source_object_id=target_ref,
                intent=intent,
                relation_kind=relation_kind,
                payload=payload,
                provenance=provenance,
                status="pending",
            )
        )

    def _apply_relation_intents(self, session: Session, target_source_object_id: str) -> int:
        """Materialize parked relate decisions that were waiting for this file's matter.

        Called at the end of classify_matter, inside its transaction, so the intents
        land atomically with the assignment. Takes the same sorted relate-file
        advisory locks as _relate's materialization; sorted acquisition on both
        sides means replay and concurrent relates serialize instead of deadlocking.
        An intent whose origin or target still cannot be resolved stays pending and
        is retried on the next classify of this file.
        """
        pending = session.scalars(
            select(RelationIntent).where(
                RelationIntent.target_source_object_id == target_source_object_id,
                RelationIntent.status == "pending",
            )
        ).all()
        if not pending:
            return 0
        refs = sorted({target_source_object_id, *{item.source_object_id for item in pending}})
        for ref in refs:
            _advisory_xact_lock(session, f"relate-file:{ref}")
        session.expire_all()
        pending = session.scalars(
            select(RelationIntent).where(
                RelationIntent.target_source_object_id == target_source_object_id,
                RelationIntent.status == "pending",
            )
        ).all()
        applied = 0
        for item in pending:
            provenance = {
                **(item.provenance or {}),
                "method": "file-scoped-ai-relation-replayed",
            }
            target_entity = self._file_entity_for_ref(
                session, target_source_object_id, provenance
            )
            origin_entity = self._file_entity_for_ref(
                session, item.source_object_id, provenance
            )
            if target_entity is None or origin_entity is None:
                continue
            payload = item.payload or {}
            if item.intent == "relation":
                from_document, to_document = (
                    (target_entity[0], origin_entity[0])
                    if payload.get("direction") == "target_to_current"
                    else (origin_entity[0], target_entity[0])
                )
                if from_document.id == to_document.id:
                    item.status = "applied"
                    item.applied_at = datetime.now(UTC)
                    continue
                _ensure_relation(
                    session,
                    from_type="document",
                    from_id=from_document.id,
                    to_type="document",
                    to_id=to_document.id,
                    kind=item.relation_kind,
                    provenance={**provenance, "evidence": [payload.get("evidence")]},
                )
            elif item.intent == "redline":
                origin_version = origin_entity[1]
                if (
                    origin_version.redline_against is None
                    and target_entity[1].id != origin_version.id
                ):
                    origin_version.redline_against = target_entity[1].id
            elif item.intent == "redline_by":
                markup_version = target_entity[1]
                if (
                    markup_version.redline_against is None
                    and origin_entity[1].id != markup_version.id
                ):
                    markup_version.redline_against = origin_entity[1].id
            elif item.intent == "thread_member":
                subject = payload.get("thread_subject")
                thread_matter_id = payload.get("matter_id")
                thread_matter = (
                    session.get(Matter, thread_matter_id) if thread_matter_id else None
                )
                if subject and thread_matter is not None:
                    thread = session.scalar(
                        select(CommunicationThread).where(
                            CommunicationThread.matter_id == thread_matter.id,
                            CommunicationThread.subject_norm == subject,
                        )
                    )
                    if thread is None:
                        thread = CommunicationThread(
                            matter_id=thread_matter.id, subject_norm=subject
                        )
                        session.add(thread)
                        session.flush()
                    _ensure_relation(
                        session,
                        from_type="document",
                        from_id=target_entity[0].id,
                        to_type="thread",
                        to_id=thread.id,
                        kind=RelationKind.BELONGS_TO_THREAD.value,
                        provenance=provenance,
                    )
                    session.flush()
                    # the target may meanwhile sit in its own thread — one document
                    # in two threads of a matter proves they are one conversation
                    self._unify_document_threads(
                        session, thread_matter, target_entity[0].id, thread
                    )
            elif item.intent == "identity":
                # The origin file declared itself a version/copy of this file while it
                # was unclassified and was materialized standalone. Merge it now,
                # mirroring the live identity branch: move (or share) the version,
                # fold the abandoned standalone document into the survivor, and
                # requeue the origin's knowledge stages so its metadata/chunks are
                # re-derived under the surviving document.
                origin_document, origin_version = origin_entity
                target_document, target_version = target_entity
                if origin_document.id == target_document.id:
                    item.status = "applied"
                    item.applied_at = datetime.now(UTC)
                    applied += 1
                    continue
                origin_source = session.get(SourceObject, item.source_object_id)
                if origin_source is None:
                    continue
                ghost_document_id, ghost_version_id = origin_document.id, origin_version.id
                if payload.get("identity") == "duplicate":
                    _relink_version_source(
                        session, target_version.id, item.source_object_id
                    )
                    survivor_version = target_version
                else:  # new_version
                    if (
                        origin_version.id == target_version.id
                        or _version_source_count(session, origin_version.id) > 1
                    ):
                        origin_version = self._create_version(
                            session, target_document, origin_source, provenance
                        )
                        _relink_version_source(
                            session, origin_version.id, item.source_object_id
                        )
                    else:
                        origin_version.document_id = target_document.id
                    survivor_version = origin_version
                    self._order_file_version(
                        session,
                        target_document,
                        origin_version,
                        target_version,
                        payload.get("relative_order") or "unknown",
                        provenance,
                    )
                session.flush()
                self._absorb_abandoned_entity(
                    session,
                    ghost_document_id=ghost_document_id,
                    ghost_version_id=ghost_version_id,
                    survivor_document_id=target_document.id,
                    survivor_version_id=survivor_version.id,
                )
                _refresh_latest_final(session, target_document)
                self._requeue_knowledge_stages(session, item.source_object_id)
            item.status = "applied"
            item.applied_at = datetime.now(UTC)
            applied += 1
        return applied

    def _requeue_knowledge_stages(self, session: Session, source_object_id: str) -> None:
        """Re-derive a file's knowledge stages after a replayed identity merge.

        The origin file was fully or partly processed as a standalone document that
        the merge just deleted: its metadata was written onto the dead document and
        its chunks are indexed under the dead document's id. Requeue the first
        already-DONE stage from extract_metadata onward (the shared invalidation
        helper parks everything downstream behind it), so the pipeline re-derives
        them under the surviving document. Stages that are still pending/running are
        left alone — they will run against the survivor anyway, and a running claim
        must never be reset underneath its worker.
        """
        stage_names = [stage.value for stage in PIPELINE_STAGE_ORDER]
        rows = {
            row.stage: row
            for row in session.scalars(
                select(ProcessingState).where(
                    ProcessingState.source_object_id == source_object_id
                )
            ).all()
        }
        start = stage_names.index(PipelineStage.EXTRACT_METADATA.value)
        for index in range(start, len(stage_names)):
            row = rows.get(stage_names[index])
            if row is not None and row.status == ProcessingStatus.DONE.value:
                self._requeue_stage_and_downstream(rows, stage_names, index)
                return

    def ensure_source_object_ready(self, source_object_id: str) -> dict:
        """Bring a neighbour file to 'converted' through the normal pipeline machinery.

        Runs the target's fetch/convert via the same claim path Hatchet workers use
        (run_stage_for_object), so a stage can never run twice, quarantine/retry
        bookkeeping stays identical, and the file's own workflow later sees DONE and
        no-ops. Best-effort by design: bounded by a time budget and a process-wide
        gate; every non-ready outcome is an honest status the relation agent can act
        on — the pair is then linked from the other file's side.
        """
        budget = self.config.pipeline.inline_conversion_budget_seconds
        gate = _inline_conversion_gate(self.config.pipeline.inline_conversion_slots)
        if not gate.acquire(blocking=False):
            return {"status": "busy"}
        deadline = time.monotonic() + budget
        try:
            for stage in _INLINE_READY_STAGES:
                while True:
                    snapshot = self._stage_snapshot(source_object_id, stage)
                    if snapshot is None:
                        return {"status": "untracked"}
                    status, retry_due = snapshot
                    if status in {
                        ProcessingStatus.DONE.value,
                        ProcessingStatus.SKIPPED.value,
                    }:
                        break
                    if status == ProcessingStatus.QUARANTINED.value:
                        return {"status": "quarantined"}
                    if status == ProcessingStatus.FAILED.value and not retry_due:
                        return {"status": "failed_retry_scheduled"}
                    if status == ProcessingStatus.RUNNING.value:
                        # another worker holds the claim — observe, never duplicate
                        if time.monotonic() >= deadline:
                            return {"status": "in_progress"}
                        time.sleep(1.0)
                        continue
                    # PENDING, or FAILED with its retry due: run the stage here, on
                    # this worker's already-allocated slot.
                    run = self.run_stage_for_object(stage, source_object_id)
                    if run.processed:
                        log.info(
                            "relate pulled %s forward for %s (done=%d retried=%d quarantined=%d)",
                            stage,
                            source_object_id,
                            run.done,
                            run.retried,
                            run.quarantined,
                        )
                        continue
                    # lost the claim race to a worker; observe the winner instead
                    if time.monotonic() >= deadline:
                        return {"status": "in_progress"}
                    time.sleep(0.5)
            return {"status": "ready"}
        finally:
            gate.release()

    def _stage_snapshot(self, source_object_id: str, stage: str) -> tuple[str, bool] | None:
        with self.session_factory() as session:
            state = session.scalar(
                select(ProcessingState).where(
                    ProcessingState.source_object_id == source_object_id,
                    ProcessingState.stage == stage,
                )
            )
            if state is None:
                return None
            retry_due = state.next_retry_at is None or state.next_retry_at <= datetime.now(UTC)
            return state.status, retry_due

    def _file_entity_for_ref(
        self, session: Session, source_object_id: str | None, provenance: dict
    ) -> tuple[Document, DocumentVersion] | None:
        if not source_object_id:
            return None
        source_object = session.get(SourceObject, source_object_id)
        if source_object is None or source_object.deleted_at is not None or not source_object.content_hash:
            return None
        assignment = session.get(MatterAssignment, source_object.id)
        matter = session.get(Matter, assignment.matter_id) if assignment else None
        if matter is None:
            return None
        return self._ensure_file_entity(session, source_object, matter, provenance)

    def _ensure_file_entity(
        self,
        session: Session,
        source_object: SourceObject,
        matter: Matter,
        provenance: dict,
    ) -> tuple[Document, DocumentVersion]:
        version = _linked_version(session, source_object.id)
        document = session.get(Document, version.document_id) if version else None
        if document is not None and version is not None:
            return document, version
        return self._create_file_entity(session, source_object, matter, provenance)

    def _create_file_entity(
        self,
        session: Session,
        source_object: SourceObject,
        matter: Matter,
        provenance: dict,
    ) -> tuple[Document, DocumentVersion]:
        classification = _artifact(session, source_object.content_hash, "classification")
        payload = classification.payload or {} if classification else {}
        document = Document(
            project_id=matter.project_id,
            matter_id=matter.id,
            title=payload.get("logical_title") or source_object.name,
            provenance=provenance,
        )
        session.add(document)
        session.flush()
        version = self._create_version(session, document, source_object, provenance)
        return document, version

    def _create_version(
        self,
        session: Session,
        document: Document,
        source_object: SourceObject,
        provenance: dict,
    ) -> DocumentVersion:
        version = DocumentVersion(
            document_id=document.id,
            content_hash=source_object.content_hash,
            ordinal=1,
            status=VersionStatus.UNKNOWN.value,
            status_evidence={"related_from": source_object.id},
            provenance=provenance,
        )
        session.add(version)
        session.flush()
        _relink_version_source(session, version.id, source_object.id)
        return version

    def _order_file_version(
        self,
        session: Session,
        document: Document,
        current: DocumentVersion,
        target: DocumentVersion,
        relative_order: str,
        provenance: dict,
    ) -> None:
        """Place the current version relative to its anchor — and only as far as the
        model actually said. 'before'/'after' insert ADJACENT to the anchor (arrival
        order must not decide chain positions) and assert one pairwise SUPERSEDES
        edge. 'same' shares the anchor's ordinal. 'unknown' records exactly that: a
        NULL ordinal and no supersession — an honest gap beats a fabricated order.
        Any earlier SUPERSEDES edge between the pair that contradicts this answer is
        removed, so a re-relate replaces its old decision instead of accreting."""
        provenance = {**provenance, "relative_order": relative_order}
        versions = session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document.id)
        ).all()
        keep = None
        if relative_order == "before":
            target_ordinal = target.ordinal or 1
            for version in versions:
                if version.id != current.id and (version.ordinal or 1) >= target_ordinal:
                    version.ordinal = (version.ordinal or 1) + 1
            current.ordinal = target_ordinal
            keep = (target.id, current.id)  # supersedes: newer -> older
        elif relative_order == "after":
            target_ordinal = target.ordinal or 1
            for version in versions:
                if version.id != current.id and (version.ordinal or 1) > target_ordinal:
                    version.ordinal = (version.ordinal or 1) + 1
            current.ordinal = target_ordinal + 1
            keep = (current.id, target.id)
        elif relative_order == "same":
            current.ordinal = target.ordinal
        else:  # unknown: position is not known, and no supersession may be implied
            current.ordinal = None
        for from_id, to_id in ((current.id, target.id), (target.id, current.id)):
            if (from_id, to_id) == keep:
                continue
            session.execute(
                delete(Relation).where(
                    Relation.from_type == "document_version",
                    Relation.from_id == from_id,
                    Relation.to_type == "document_version",
                    Relation.to_id == to_id,
                    Relation.kind == RelationKind.SUPERSEDES.value,
                )
            )
        if keep is not None:
            _ensure_relation(
                session,
                from_type="document_version",
                from_id=keep[0],
                to_type="document_version",
                to_id=keep[1],
                kind=RelationKind.SUPERSEDES.value,
                provenance=provenance,
            )
        # Placement above is pairwise against ONE anchor; renumbering folds that
        # decision into the chain as a whole so the ordinals stay dense and stable.
        _renumber_chain(session, document)

    def _extract_metadata(self, session: Session, state: ProcessingState) -> StageResult:
        # Runs on EVERY version: this stage is the sole owner of document typing
        # (the ontology walk), so drafts get typed too. The clause pass stays
        # restricted to final/executed versions via the prompt contract.
        source_object, document, version = _knowledge_entities(session, state)
        converted = _required_artifact(session, source_object.content_hash, "structured_json")
        text = (converted.payload or {}).get("text", "")
        model = self.config.pipeline.stage("extract_metadata").model
        prompt_version = self.config.pipeline.stage("extract_metadata").producer_version
        scope = self.config.doc_ontology()
        clause_scope = (
            self.config.ontology_facet("clause")
            if "clause" in self.config.ontology.active_facets
            else None
        )
        trace_id, trace_tags = _stage_trace(source_object.id, "extract_metadata")
        visited: set[str] = set()
        clause_visited: set[str] = set()
        seen_ids: set[str] = set()
        searched_queries: set[str] = set()

        def validate_metadata(candidate: DocumentMetadata) -> str | None:
            if candidate.type_node is not None:
                if candidate.type_node not in visited:
                    return (
                        f"type_node {candidate.type_node!r} did not appear in any ontology "
                        "tool result; navigate with ontology_roots/ontology_children and "
                        "submit a node id you have seen, or null if nothing fits"
                    )
                if candidate.type_node not in scope.visible:
                    return f"type_node {candidate.type_node!r} is not part of the active ontology"
            for clause in candidate.notable_clauses:
                node = clause.clause_type_node
                if node is None:
                    continue  # an untyped clause is an honest, valid outcome
                if clause_scope is None or node not in clause_visited:
                    return (
                        f"clause_type_node {node!r} ({clause.locus!r}) did not appear in "
                        "any clause_search result; look the clause type up with "
                        "clause_search and submit an id you have seen, or null"
                    )
                if node not in clause_scope.visible:
                    return f"clause_type_node {node!r} is not part of the active clause facet"
            # Both guards are about what the agent SAW, not about what is stored: a
            # submitted existing_id has to come from a search result, and a party the
            # agent never searched for cannot be asserted as new. Whether two names
            # are one entity is decided afterwards, by rule, in
            # _resolve_document_parties — so neither of these is load-bearing for
            # deduplication any more, and neither ever was.
            for party in candidate.parties:
                if party.existing_id and party.existing_id not in seen_ids:
                    return (
                        f"party {party.name!r} has existing_id {party.existing_id!r} that did "
                        "not appear in any search_entities result; search first and reuse only "
                        "an id you have seen, or set existing_id to null to create a new party"
                    )
                if party.existing_id is None and not entity_search_covered(
                    party.name, searched_queries
                ):
                    return (
                        f"party {party.name!r} would create a NEW entity, but no "
                        "search_entities call looked for it; search for this party first, "
                        "then reuse a matching candidate's id — or keep existing_id null "
                        "if the search confirms the firm does not know it yet"
                    )
            return None

        tools = ontology_navigation_tools(scope, visited)
        if clause_scope is not None:
            tools.append(clause_search_tool(clause_scope, clause_visited))
        tools.extend(
            party_resolution_tools(
                session, seen_ids, searched_queries, matter_id=document.matter_id
            )
        )
        metadata = chat_agent(
            model,
            self.config,
            system=METADATA_SYSTEM,
            user=json.dumps(
                {
                    "filename": source_object.name,
                    "version_status": version.status,
                    "text": text[:16000],
                },
                ensure_ascii=False,
            ),
            tools=tools,
            final_schema=DocumentMetadata,
            # Reasoning models account internal reasoning against max_tokens. Keep enough
            # room for both reasoning and the dense, clause-heavy JSON response.
            # Ontology listings are bounded (~25KB for the largest sibling list) and
            # must NEVER be cut: a truncated list means the right candidate is
            # invisible and the walk fails silently. Effectively no truncation.
            result_validator=validate_metadata,
            trace_tags=trace_tags,
        )
        if metadata.type_node is None:
            # The ontology has no concept for this document's genre. Untyped is
            # recorded honestly; the fingerprint still marks which scope judged
            # it, so a richer artifact later re-types exactly these documents.
            type_node = None
            document.doc_type = None
            document.doc_type_ancestors = []
        else:
            type_node = scope.resolve(metadata.type_node)
            if type_node is None:
                raise ModelOutputInvalid(
                    f"type_node {metadata.type_node!r} is not in the ontology"
                )
            document.doc_type = type_node
            document.doc_type_ancestors = sorted(scope.ancestors(type_node))
        document.ontology_fingerprint = scope.fingerprint
        document.language = metadata.language
        content_date = _parse_iso_date(metadata.doc_date)
        # mtime is a fact about how we handled the file, not about the document.
        # For managed imports (local_fs copies files into appdata) mtime is the
        # copy time — i.e. the ingestion day — so stamping it as doc_date is wrong
        # data that misleads every date_from/date_to filter and the doc_date-sorted
        # metadata search. Trust mtime only when the connector marks it a real
        # document date (Source.config.trust_mtime); otherwise leave doc_date null,
        # which is honest ("undated") rather than confidently wrong.
        source_config = getattr(source_object.source, "config", None) or {}
        trust_mtime = bool(source_config.get("trust_mtime", False))
        fallback_date = source_object.mtime if trust_mtime else None
        document.doc_date = content_date or fallback_date
        if content_date:
            doc_date_source = "document_content"
        elif fallback_date is not None:
            doc_date_source = "file_mtime"
        else:
            doc_date_source = "none"
        document.title = metadata.title or document.title
        document.parties = _resolve_document_parties(
            session,
            document,
            metadata.parties,
            model=model,
            evidence=source_object.id,
            session_factory=self.session_factory,
            config=self.config,
        )
        document.identifiers = sorted(
            {value.strip() for value in metadata.identifiers if value.strip()}
        )
        document.provenance = {
            "model": model,
            "prompt_version": prompt_version,
            "confidence": metadata.confidence,
            "evidence": [source_object.id],
            "doc_date_source": doc_date_source,
            "ontology_fingerprint": scope.fingerprint,
            "type_path": scope.path_labels(type_node) if type_node else None,
            "trace_id": trace_id,
        }
        # a document date landed (or changed): re-derive the matter's activity span
        _refresh_matter_time_range(
            session,
            session.get(Matter, document.matter_id) if document.matter_id else None,
        )
        # persist the model-identified clauses for the clause-embedding index rows
        if _artifact(session, source_object.content_hash, "notable_clauses") is None:
            session.add(
                Artifact(
                    content_hash=source_object.content_hash,
                    producer="extract-metadata-llm",
                    producer_version=prompt_version,
                    kind="notable_clauses",
                    payload={"clauses": _typed_clauses(clause_scope, metadata.notable_clauses)},
                )
            )
        if not session.scalar(
            select(Extraction).where(
                Extraction.target_entity == "document", Extraction.target_id == document.id
            )
        ):
            session.add(
                Extraction(
                    target_entity="document",
                    target_id=document.id,
                    fields=["doc_type", "language", "doc_date", "title", "parties"],
                    model=model,
                    prompt_version=prompt_version,
                    input_artifact_refs=[converted.id],
                    confidence=metadata.confidence,
                )
            )
        return StageResult()

    def _extract_decisions(self, session: Session, state: ProcessingState) -> StageResult:
        source_object, document, version = _knowledge_entities(session, state)
        converted = _required_artifact(session, source_object.content_hash, "structured_json")
        payload = converted.payload or {}
        revisions = payload.get("revisions") or []
        comments = (payload.get("metadata") or {}).get("comments") or []
        if not revisions and not comments:
            return StageResult("no_revision_evidence")
        existing = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.document_id == document.id,
                DecisionRecord.version_to == version.id,
            )
        )
        if existing is not None:
            return StageResult()
        model = self.config.pipeline.stage("extract_decisions").model
        trace_id, trace_tags = _stage_trace(source_object.id, "extract_decisions")
        result = chat_json(
            model,
            self.config,
            system=DECISION_SYSTEM,
            user=json.dumps(
                {
                    "document_title": document.title,
                    "revisions": revisions[:200],
                    "comments": comments[:100],
                    "text_head": str(payload.get("text") or "")[:6000],
                },
                ensure_ascii=False,
            ),
            schema=DecisionExtraction,
            trace_tags=trace_tags,
        )
        if not result.has_decision:
            return StageResult("no_decision_evidence")
        if result.rationale_category not in {item.value for item in RationaleCategory}:
            raise ModelOutputInvalid(
                f"rationale_category {result.rationale_category!r} is not in the taxonomy"
            )
        if not (result.rationale_text or "").strip():
            raise ModelOutputInvalid("decision extraction returned an empty rationale")
        session.add(
            DecisionRecord(
                matter_id=document.matter_id,
                document_id=document.id,
                version_from=version.redline_against,
                version_to=version.id,
                locus=result.locus,
                change_summary=result.change_summary,
                rationale_category=result.rationale_category,
                rationale_text=result.rationale_text,
                generalizable=result.generalizable,
                source_evidence=[{"source_object_id": source_object.id, "artifact": converted.id}],
                provenance={
                    "model": model,
                    "prompt_version": self.config.pipeline.stage(
                        "extract_decisions"
                    ).producer_version,
                    "confidence": result.confidence,
                    "evidence": [converted.id],
                    "trace_id": trace_id,
                },
            )
        )
        return StageResult()

    def _generate_evals(self, session: Session, state: ProcessingState) -> StageResult:
        # RL-environment generation moved out of the insertion pipeline into the sparse,
        # human-in-the-loop EnvironmentBuilder (pipeline/environments.py). GEN_EVALS is no
        # longer in PIPELINE_STAGE_ORDER; this handler remains only to drain any orphaned
        # gen_evals states left by earlier runs.
        del session, state
        return StageResult("gen_evals_moved_to_environment_builder")

    def _index(self, session: Session, state: ProcessingState) -> StageResult:
        source_object, document, version = _knowledge_entities(session, state)
        # A merged version is fed by several source objects, each with its own
        # index task; serialize them (like relate does per file ref) so the
        # read-then-diff below never runs twice concurrently on one version.
        _advisory_xact_lock(session, f"index-version:{version.id}")
        access_only = (state.last_error or {}).get("reason") == ACCESS_ONLY_REINDEX
        converted = _required_artifact(session, source_object.content_hash, "structured_json")
        text = (converted.payload or {}).get("text", "")
        retrieval = self.config.retrieval
        matter = session.get(Matter, document.matter_id) if document.matter_id else None
        # Prose surfaces (context headers, profile text) get the human label;
        # filter columns keep the ontology node id.
        scope = self.config.doc_ontology()
        doc_type_label = scope.label_of(document.doc_type) if document.doc_type else None
        header = (
            context_header(
                title=document.title,
                doc_type=doc_type_label,
                matter_title=matter.title if matter else None,
            )
            if retrieval.chunk_contextualize
            else ""
        )
        is_final = version.status in {VersionStatus.FINAL.value, VersionStatus.EXECUTED.value}
        is_latest_final = document.latest_final_version_id == version.id

        # (ordinal, text, meta) rows to index. Body chunks 0..n; profile row at -1;
        # clause rows at 1000+. All three participate in the existing-chunk diff so
        # re-indexing replaces rather than orphans them.
        rows: list[tuple[int, str, dict]] = [
            (ordinal, value, {"source_object_id": source_object.id, "kind": "chunk"})
            for ordinal, value in enumerate(
                split_text(text, size=retrieval.chunk_chars, overlap=retrieval.chunk_overlap_chars)
            )
        ]
        if retrieval.profile_embeddings and is_latest_final:
            profile = build_profile_text(
                title=document.title,
                doc_type=doc_type_label,
                matter_title=matter.title if matter else None,
                reference_numbers=(matter.reference_numbers or []) if matter else [],
                parties=[p.get("name", "") for p in (document.parties or [])],
                identifiers=document.identifiers or [],
                doc_date=document.doc_date.isoformat() if document.doc_date else None,
                text=text,
            )
            rows.append((-1, profile, {"source_object_id": source_object.id, "kind": "profile"}))
        if retrieval.clause_embeddings and is_final:
            clause_artifact = _artifact(session, source_object.content_hash, "notable_clauses")
            clauses = ((clause_artifact.payload or {}) if clause_artifact else {}).get("clauses", [])
            for index, clause in enumerate(clauses):
                rows.append(
                    (
                        1000 + index,
                        clause["text"],
                        {
                            "source_object_id": source_object.id,
                            "kind": "clause",
                            "locus": clause["locus"],
                            "clause_type": clause.get("clause_type"),
                        },
                    )
                )

        # Keyed by ordinal for the re-index diff; a duplicate ordinal (a version
        # double-indexed before the unique constraint / lock existed) would be
        # shadowed by the dict and become undeletable — collect and heal instead.
        existing: dict[int, Chunk] = {}
        duplicate_chunks: list[Chunk] = []
        for chunk in session.scalars(
            select(Chunk)
            .where(Chunk.document_version_id == version.id)
            .order_by(Chunk.ordinal, Chunk.id)
        ):
            if chunk.ordinal in existing:
                duplicate_chunks.append(chunk)
            else:
                existing[chunk.ordinal] = chunk
        source_grants = session.scalars(
            select(SourceObjectGrant)
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObjectGrant.source_object_id,
            )
            .where(DocumentVersionSource.version_id == version.id)
        ).all()
        project_grants = (
            session.scalars(
                select(ProjectGrant).where(ProjectGrant.project_id == document.project_id)
            ).all()
            if document.project_id
            else []
        )
        document_grants = session.scalars(
            select(DocumentGrant).where(DocumentGrant.document_id == document.id)
        ).all()
        effective_grants = [*project_grants, *document_grants, *source_grants]
        allowed_principals = sorted(
            {grant.principal for grant in effective_grants if grant.effect == "allow"}
        )
        denied_principals = sorted(
            {grant.principal for grant in effective_grants if grant.effect == "deny"}
        )
        if access_only and existing:
            # Authorization is compiled authoritatively from SQL at query time, while
            # these chunk fields are a denormalized projection for inspection/export.
            # Keep that projection current without splitting text or calling the
            # embedding model again when only the source ACL changed.
            active_chunks = list(existing.values())
            for chunk in active_chunks:
                chunk.allowed_principals = allowed_principals
                chunk.denied_principals = denied_principals
                chunk.access_version = (chunk.access_version or 0) + 1
            duplicate_ids = [chunk.id for chunk in duplicate_chunks]
            for duplicate in duplicate_chunks:
                session.delete(duplicate)
            from knowledge_index.search_backend import OpenSearchIndex

            session.flush()
            OpenSearchIndex(self.config).bulk_sync(deletes=duplicate_ids, upserts=active_chunks)
            return StageResult()

        active_chunks: list[Chunk] = []
        for ordinal, value, meta in rows:
            chunk = existing.pop(ordinal, None)
            if chunk is None:
                chunk = Chunk(document_version_id=version.id, ordinal=ordinal, text=value)
                session.add(chunk)
            chunk.text = value  # raw text for display/excerpts
            chunk.meta = meta
            chunk.project_id = document.project_id
            chunk.document_id = document.id
            chunk.matter_id = document.matter_id
            chunk.doc_type = document.doc_type
            chunk.doc_type_ancestors = document.doc_type_ancestors or []
            chunk.version_status = version.status
            chunk.language = document.language
            chunk.doc_date = document.doc_date
            chunk.identifiers = document.identifiers or []
            # F4 party filter: index each party's resolved id AND canonical name as
            # keyword terms, so a caller can filter by either. Deduped/sorted for a
            # stable OpenSearch body (unchanged re-index → no-op write).
            chunk.parties = sorted(
                {
                    term
                    for party in (document.parties or [])
                    for term in (party.get("party_id"), party.get("name"))
                    if term
                }
            )
            chunk.allowed_principals = allowed_principals
            chunk.denied_principals = denied_principals
            chunk.access_version = 1
            # embed the context-prefixed string; store raw text above
            chunk.embedding = embed_text(contextualize(value, header), self.config)
            chunk.embedding_model = self.config.retrieval.embedding_model
            active_chunks.append(chunk)
        obsolete_chunks = [*existing.values(), *duplicate_chunks]
        removed_chunk_ids = [chunk.id for chunk in obsolete_chunks]
        for obsolete in obsolete_chunks:
            session.delete(obsolete)
        from knowledge_index.search_backend import OpenSearchIndex

        session.flush()
        OpenSearchIndex(self.config).bulk_sync(deletes=removed_chunk_ids, upserts=active_chunks)
        return StageResult()

    @staticmethod
    def _unlock_next(session: Session, state: ProcessingState) -> None:
        stages = [stage.value for stage in PIPELINE_STAGE_ORDER]
        index = stages.index(state.stage)
        if index == len(stages) - 1:
            return
        next_stage = stages[index + 1]
        row = session.scalar(
            select(ProcessingState).where(
                ProcessingState.source_object_id == state.source_object_id,
                ProcessingState.stage == next_stage,
            )
        )
        if row is not None and row.status == ProcessingStatus.SKIPPED.value:
            if (row.last_error or {}).get("reason") == WAITING_FOR_PREVIOUS_STAGE:
                row.status = ProcessingStatus.PENDING.value
                row.last_error = None


def _record_matter_vote(
    matter: Matter, *, area: str | None, kind: str | None, weight: float
) -> None:
    """Add one document's opinion to the matter's tally and re-derive the labels.

    The classify agent sees one document, so its answer is evidence about the
    matter rather than a verdict on it. Tallying the evidence and taking the mode
    makes the label a property of the whole matter and makes arrival order stop
    mattering — the two things "first valid answer wins" got wrong.

    ``weight`` is the agent's own confidence in that answer, so a document it
    classified reluctantly counts for less than one it was sure about. Votes live
    in ``provenance`` (already JSON) so this needs no migration, and they are kept
    rather than reduced away: they are the audit trail for why a matter carries
    the label it does, and they let a later document change a wrong early call.
    """
    if weight <= 0:
        # The agent said it was guessing. Recording a zero-weight vote would still
        # let enough guesses outvote one confident answer.
        return
    votes = dict(matter.provenance or {})
    for field, value in (("area_votes", area), ("kind_votes", kind)):
        if not value:
            continue
        tally = dict(votes.get(field) or {})
        tally[value] = round(tally.get(value, 0.0) + weight, 4)
        votes[field] = tally
        # Ties keep the incumbent: re-running the same corpus must not shuffle a
        # matter between two equally-supported areas.
        current = matter.practice_area if field == "area_votes" else matter.matter_kind
        winner = max(tally, key=lambda node: (tally[node], node == current))
        if field == "area_votes":
            matter.practice_area = winner
        else:
            matter.matter_kind = winner
        # A label the matter's own documents do not agree on is a label to review,
        # not one to trust. This is the signal the old first-wins path destroyed:
        # it recorded a winner and threw the disagreement away, so a matter split
        # between Tax and Funds looked exactly like one every document agreed on.
        total = sum(tally.values())
        votes.setdefault("contested", {})[field] = round(
            1.0 - (tally[winner] / total), 3
        ) if total else 0.0
    matter.provenance = votes


def _advisory_xact_lock(session: Session, key: str) -> None:
    """Serialize one logical entity on Postgres for the current transaction only."""
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    lock_id = int.from_bytes(
        hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
    session.execute(select(func.pg_advisory_xact_lock(lock_id)))


def connector_from_source(source: Source, session: Session | None = None) -> SyncSource:
    """Build the connector for one configured source.

    Externally hosted sources resolve through the connector registry, so adding a
    connector never touches this function.
    """
    config = source.config or {}
    if source.kind not in {"local_fs", "plugin_drop"}:
        from knowledge_index.connectors import build_connector, credentials as credential_store

        if session is None:
            raise ValueError(
                f"source {source.kind!r} needs stored credentials; "
                "connector_from_source must be given a session"
            )
        stored = credential_store.load(session, source.id)

        def persist(updated: dict) -> None:
            # Called from the token provider when a refresh rotates the token. Written
            # immediately so a later crash cannot strand the connection on a refresh
            # token the provider has already invalidated.
            credential_store.save(session, source.id, updated, provider=source.kind)

        from knowledge_index.connectors import scoping

        return build_connector(
            short_name=source.kind,
            config=config.get("connector") or {},
            credentials=stored,
            cursor_data=_cursor_data(source),
            node_selections=scoping.to_node_selections(config.get("connector")),
            persist_credentials=persist,
            run_id=source.id,
            allow_private_hosts=bool(config.get("allow_private_hosts")),
        )
    if source.kind == "plugin_drop":
        # FDE-authored drop directory (see docs/src/content/docs/development/plugin-connectors.md)
        return PluginDropSource(config["root"])
    acl_map = config.get("acl_by_path", {})

    def acl_resolver(path: Path) -> list[dict] | None:
        root = Path(config["root"]).resolve()
        relative = path.relative_to(root).as_posix()
        return acl_map.get(relative, config.get("default_acl"))

    has_acl = bool(acl_map) or "default_acl" in config
    return LocalFilesystemSource(config["root"], acl_resolver=acl_resolver if has_acl else None)


def _effective_party_role(
    session: Session,
    matter: Matter | None,
    party: ExtractedParty,
    role: str,
    config: AppConfig,
) -> tuple[str, str | None]:
    """The role a mention actually gets, and why it is not the one claimed.

    ``client`` does not mean "an important company on this page" — it means the party
    THIS FIRM acts for on THIS matter. The 9,288-document run had 985 distinct names
    carrying it, including individuals, counterparties, co-investors, and the firm
    itself 16 times. Three refusals, none of them a prompt:

    * the firm is never its own client (config.firm; off when unset, because an
      appliance that has not been told whose it is cannot recognise its own name);
    * an entity already recorded as a party on this matter keeps the role it has —
      nobody is the opposing party and the client of the same matter;
    * a matter whose client came from practice management is authoritative, so a
      document-level claim that disagrees is recorded as a named party instead.
    """
    if role != PartyRole.CLIENT.value:
        return role, None
    if config.firm.is_self(party.name):
        # Recorded as the advising law firm, which is what it is, rather than dropped:
        # it IS on the document.
        return PartyRole.ADVISOR.value, "own_firm"
    if matter is None:
        return role, None
    normalized = normalize_entity_name(party.name)
    incumbent = session.scalar(
        select(MatterParty.role)
        .join(Party, Party.id == MatterParty.party_id)
        .where(MatterParty.matter_id == matter.id, Party.normalized_name == normalized)
        .order_by(MatterParty.role)
        .limit(1)
    )
    if incumbent:
        return incumbent, "already_a_party_on_this_matter"
    if matter.imported:
        authoritative = set(
            session.scalars(
                select(Client.normalized_name)
                .join(MatterClient, MatterClient.client_id == Client.id)
                .where(MatterClient.matter_id == matter.id)
            )
        )
        if authoritative and normalized not in authoritative:
            return PartyRole.OTHER.value, "matter_client_is_authoritative"
    return role, None


def _resolve_document_parties(
    session: Session,
    document: Document,
    parties: list[ExtractedParty],
    *,
    model: str,
    evidence: str,
    session_factory: sessionmaker[Session],
    config: AppConfig,
) -> list[dict]:
    """Materialize the document's named parties into the firm-wide entity layer.

    Resolution is NOT the agent's to decline. It searched, and it may have linked by
    id; but whether two names are one entity is a rule (see
    ``matter_search.link_decision``), applied here to every mention regardless of what
    the agent submitted. That is the difference between a client with five matters and
    five clients with one matter each, and the corpus this was measured on had 1,076 of
    1,212 clients touching exactly one matter where the ground truth is 46 clients
    across 266 matters.

    Each entity is resolved-or-created in its OWN committed transaction, so a sibling
    document extracting the same party in parallel sees it immediately instead of
    creating its own copy minutes later. The matter links written here belong to this
    stage's transaction, because they are facts about this document.

    Returns the ``document.parties`` payload with each mention's resolved entity id.
    """
    matter_id = document.matter_id
    matter = session.get(Matter, matter_id) if matter_id else None
    provenance = {"method": "inferred", "model": model, "evidence": [evidence]}
    resolved: list[dict] = []
    # Entities already resolved on THIS document corroborate the next one: a
    # candidate that shares a matter with a party named beside it here is very
    # likely the same entity as the name that reached it.
    siblings: set[str] = set()
    # Now that two mentions of one name resolve to ONE entity, a document naming it
    # twice reaches the same link twice; session.get cannot see the first one while
    # it is still pending, so the pair is remembered here instead.
    linked: set[tuple[str, str]] = set()
    for party in parties:
        claimed = party.role.value if isinstance(party.role, PartyRole) else str(party.role)
        role, demotion = _effective_party_role(session, matter, party, claimed, config)
        is_client = role == PartyRole.CLIENT.value
        entity_type = "client" if is_client else "party"
        outcome = resolve_or_create_entity(
            session_factory,
            entity_type=entity_type,
            name=party.name,
            kind=party.kind,
            identifiers={ident.scheme: ident.value for ident in party.identifiers},
            provenance=provenance,
            matter_id=matter_id,
            sibling_entity_ids=siblings,
            preferred_entity_id=party.existing_id,
        )
        entity_id = outcome["id"]
        siblings.add(entity_id)

        if matter_id and (entity_id, role) not in linked:
            linked.add((entity_id, role))
            if is_client:
                if session.get(MatterClient, (matter_id, entity_id)) is None:
                    session.add(MatterClient(matter_id=matter_id, client_id=entity_id))
            elif session.get(MatterParty, (matter_id, entity_id, role)) is None:
                session.add(
                    MatterParty(
                        matter_id=matter_id,
                        party_id=entity_id,
                        role=role,
                        provenance=provenance,
                    )
                )
        mention = {
            "name": party.name,
            "role": role,
            "entity_type": entity_type,
            "party_id": entity_id,
            "resolution": outcome["reason"],
        }
        if demotion:
            # The claim is kept next to the refusal: an operator reading this row
            # should see what the model said, not only what was stored.
            mention["claimed_role"] = claimed
            mention["role_refused_because"] = demotion
        resolved.append(mention)
    return resolved

def _close_connectors(connectors: dict) -> None:
    for connector in connectors.values():
        closer = getattr(connector, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown must not mask a pipeline result
                pass
    connectors.clear()


def _cursor_data(source: Source) -> dict | None:
    """Decode the opaque cursor the engine persisted for this source."""
    if not source.cursor:
        return None
    try:
        decoded = json.loads(source.cursor)
    except (TypeError, ValueError):
        # A cursor we cannot parse must not silently become "no cursor" — that would
        # turn an incremental sync into a full rescan without anyone noticing.
        raise ValueError(
            f"source {source.id} has an unreadable cursor; force a full sync to reset it"
        ) from None
    return decoded if isinstance(decoded, dict) else None


def _source_object_with_hash(session: Session, state: ProcessingState) -> SourceObject:
    source_object = session.get(SourceObject, state.source_object_id)
    if source_object is None or not source_object.content_hash:
        raise RetryableStageError("source object has not been fetched")
    return source_object


def _artifact(session: Session, content_hash: str, kind: str) -> Artifact | None:
    return session.scalar(
        select(Artifact)
        .where(Artifact.content_hash == content_hash, Artifact.kind == kind)
        .order_by(Artifact.created_at.desc())
    )


def _required_artifact(session: Session, content_hash: str, kind: str) -> Artifact:
    result = _artifact(session, content_hash, kind)
    if result is None:
        raise RetryableStageError(f"required {kind} artifact is missing")
    return result


def _relink_version_source(session: Session, version_id: str, source_object_id: str) -> None:
    """Link a source object to exactly one version (regroupings may move it)."""
    existing = session.scalars(
        select(DocumentVersionSource).where(
            DocumentVersionSource.source_object_id == source_object_id
        )
    ).all()
    for link in existing:
        if link.version_id != version_id:
            session.delete(link)
    if session.get(DocumentVersionSource, (version_id, source_object_id)) is None:
        session.add(DocumentVersionSource(version_id=version_id, source_object_id=source_object_id))


def _linked_version(session: Session, source_object_id: str) -> DocumentVersion | None:
    link = session.scalar(
        select(DocumentVersionSource).where(
            DocumentVersionSource.source_object_id == source_object_id
        )
    )
    return session.get(DocumentVersion, link.version_id) if link else None


def _file_relation_target_refs(result: FileRelationResult) -> set[str]:
    refs = {
        result.same_document_ref,
        result.duplicate_of,
        result.redline_of,
        result.redline_by,
        *result.thread_member_refs,
        *(relation.target_ref for relation in result.relations),
    }
    return {ref for ref in refs if ref}


def _version_is_newest(
    session: Session, document_id: str, version: DocumentVersion
) -> bool:
    """Whether this version's ordinal is the document's highest known position.

    Unknown-position versions (NULL ordinal) never count as newest."""
    if version.ordinal is None:
        return False
    ordinals = session.scalars(
        select(DocumentVersion.ordinal).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.id != version.id,
        )
    ).all()
    return all((ordinal or 0) <= version.ordinal for ordinal in ordinals)


def _version_source_count(session: Session, version_id: str) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(DocumentVersionSource)
            .where(DocumentVersionSource.version_id == version_id)
        )
        or 0
    )


def _document_is_exclusive_to_source(
    session: Session, document_id: str, source_object_id: str
) -> bool:
    refs = session.scalars(
        select(DocumentVersionSource.source_object_id)
        .join(DocumentVersion, DocumentVersion.id == DocumentVersionSource.version_id)
        .where(DocumentVersion.document_id == document_id)
    ).all()
    return bool(refs) and set(refs) == {source_object_id}


def _version_is_chain_anchor(
    session: Session, document_id: str, version_id: str
) -> bool:
    """Whether this version is the base of its document's version chain.

    The anchor is the first version by declared order (lowest ordinal; NULL —
    unknown order — ranks last; created_at then id break ties). Version ordering
    anchors every later arrival relative to an existing version, so the chain's base
    keeps the lowest ordinal no matter what order the files arrived in."""
    versions = session.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document_id)
    ).all()
    if not versions:
        return False
    anchor = min(
        versions,
        key=lambda version: (
            version.ordinal is None,
            version.ordinal if version.ordinal is not None else 0,
            version.created_at or datetime.min.replace(tzinfo=UTC),
            version.id,
        ),
    )
    return anchor.id == version_id


def _renumber_chain(session: Session, document: Document) -> None:
    """Re-derive the whole version chain's numbering after any join, split or move.

    Files are inserted individually and in parallel, so a chain is assembled from
    many independent pairwise decisions — and the numbering drifts out of shape as
    they land. A version that moves to another document leaves its ordinal behind,
    so chains are observed starting at 2 with no 1; ``before`` shifts every later
    ordinal up, so repeated inserts leave holes. A caller reading "ordinal 2" then
    cannot tell the second of five from the second of three with two gaps.

    Every relate task therefore renumbers the chain it touched, which makes the
    numbering a property of the chain's current members rather than of arrival
    order — the same set of files converges on the same numbering however they
    interleave. Only the SPACING changes: the relative order the model established
    is preserved exactly, versions that shared an ordinal (``relative_order:
    "same"``) still share one, and a NULL ordinal stays NULL because the model
    said it did not know and inventing a position is worse than an honest gap.
    """
    versions = session.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document.id)
    ).all()
    placed = [version for version in versions if version.ordinal is not None]
    if not placed:
        _refresh_latest_final(session, document)
        return
    earliest = datetime.min.replace(tzinfo=UTC)
    placed.sort(key=lambda v: (v.ordinal, v.created_at or earliest, v.id))
    next_ordinal, previous = 0, object()
    for version in placed:
        if version.ordinal != previous:  # a new rung, not a tie on the same one
            next_ordinal += 1
            previous = version.ordinal
        version.ordinal = next_ordinal
    _refresh_latest_final(session, document)


def _refresh_latest_final(session: Session, document: Document) -> None:
    versions = session.scalars(
        select(DocumentVersion).where(DocumentVersion.document_id == document.id)
    ).all()
    finals = [
        version
        for version in versions
        if version.status in {VersionStatus.FINAL.value, VersionStatus.EXECUTED.value}
    ]
    # Unknown-position finals (NULL ordinal) rank lowest; created_at breaks ties
    # deterministically instead of leaving the crown to iteration order.
    earliest = datetime.min.replace(tzinfo=UTC)
    document.latest_final_version_id = (
        max(
            finals,
            key=lambda version: (version.ordinal or 0, version.created_at or earliest),
        ).id
        if finals
        else None
    )


def _parent_folder(path: str) -> str:
    cut = path.rstrip("/").rfind("/")
    return path[:cut] if cut > 0 else ""


def _slug(value: str) -> str:
    out = []
    for char in value.upper():
        out.append(char if char.isalnum() else "-")
    return "-".join(part for part in "".join(out).split("-") if part)[:40]


def _parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


_SUBJECT_PREFIXES = ("re:", "fw:", "fwd:", "aw:", "wg:", "antw:", "sv:")


def _normalize_subject(subject: str) -> str:
    """Reply/forward prefixes stripped iteratively, whitespace collapsed."""
    value = " ".join((subject or "").split())
    lowered = value.casefold()
    while True:
        for prefix in _SUBJECT_PREFIXES:
            if lowered.startswith(prefix):
                value = value[len(prefix) :].lstrip()
                lowered = value.casefold()
                break
        else:
            return value


def _parse_message_ids(*headers: str | None) -> set[str]:
    """Every <message-id> in the given header values, angle brackets stripped."""
    ids: set[str] = set()
    for header in headers:
        for token in (header or "").split():
            cleaned = token.strip().strip("<>").strip()
            if cleaned:
                ids.add(cleaned)
    return ids


def _is_email_file(session: Session, source_object: SourceObject) -> bool:
    """Deterministic email detection: the converter that parsed it, or the file type.

    The one signal both the threading and the email-is-final status rule trust —
    never a model judgment."""
    artifact = _artifact(session, source_object.content_hash, "structured_json")
    metadata = ((artifact.payload if artifact else None) or {}).get("metadata") or {}
    return (
        metadata.get("converter") == "stdlib-email"
        or source_object.name.casefold().endswith(".eml")
    )


def _email_thread_context(session: Session, source_object: SourceObject) -> dict | None:
    """Header-derived threading evidence for an email file; None for non-email."""
    if not _is_email_file(session, source_object):
        return None
    artifact = _artifact(session, source_object.content_hash, "structured_json")
    metadata = ((artifact.payload if artifact else None) or {}).get("metadata") or {}
    participants = []
    for header in ("from", "to", "cc"):
        for part in (metadata.get(header) or "").split(","):
            cleaned = " ".join(part.split())
            if cleaned and cleaned not in participants:
                participants.append(cleaned)
    date = None
    if metadata.get("date"):
        try:
            date = parsedate_to_datetime(metadata["date"])
        except (TypeError, ValueError):
            date = None
    return {
        "subject": metadata.get("subject") or "",
        "ids": _parse_message_ids(
            metadata.get("message_id"),
            metadata.get("in_reply_to"),
            metadata.get("references"),
        ),
        "participants": participants,
        "date": date,
    }


def _refresh_matter_time_range(session: Session, matter: Matter | None) -> None:
    """Re-derive a matter's activity span from its documents' content dates.

    Deterministic aggregation (spec O6): min/max over member documents' ``doc_date``
    where the date was read from the document's CONTENT. Mtime-derived dates are
    excluded even when the connector is trusted — a file's storage timestamp says
    when it was touched, not when the matter was active. Recomputed from scratch on
    every touch (date extracted, document landing in a matter) so corrected dates
    and moved documents self-heal instead of accreting a stale span. Imported
    matters are left alone: practice management is authoritative for them. A matter
    with no content-dated documents keeps an honest NULL span.
    """
    if matter is None or matter.imported:
        return
    dates = [
        document.doc_date
        for document in session.scalars(
            select(Document).where(Document.matter_id == matter.id)
        )
        if document.doc_date is not None
        and (document.provenance or {}).get("doc_date_source") == "document_content"
    ]
    matter.time_range = (
        {"from": min(dates).isoformat(), "to": max(dates).isoformat()} if dates else None
    )


def _merge_time_range(existing: dict | None, moment: datetime | None) -> dict | None:
    if moment is None:
        return existing
    iso = moment.isoformat()
    if not existing:
        return {"from": iso, "to": iso}
    return {
        "from": min(existing.get("from") or iso, iso),
        "to": max(existing.get("to") or iso, iso),
    }


def _merge_threads(
    session: Session, survivor: CommunicationThread, ghost: CommunicationThread
) -> None:
    """Fold one thread into another: memberships re-pointed, evidence unioned."""
    _repoint_relations(session, "thread", ghost.id, survivor.id)
    survivor.message_ids = sorted(set(survivor.message_ids or []) | set(ghost.message_ids or []))
    merged = list(survivor.participants or [])
    merged.extend(p for p in (ghost.participants or []) if p not in merged)
    survivor.participants = merged
    for boundary in ((ghost.time_range or {}).get("from"), (ghost.time_range or {}).get("to")):
        if boundary:
            moment = _parse_iso_date(boundary)
            survivor.time_range = _merge_time_range(survivor.time_range, moment)
    if not survivor.subject_norm:
        survivor.subject_norm = ghost.subject_norm
    session.delete(ghost)
    session.flush()


def _repoint_relations(
    session: Session, entity_type: str, ghost_id: str, survivor_id: str
) -> None:
    """Move every relation edge touching a soon-deleted entity to its survivor.

    An edge that would duplicate an existing one (uq_relation) or collapse into a
    self-loop — e.g. a stale SUPERSEDES between a version and the version it just
    merged into — is deleted instead of moved."""
    rows = session.scalars(
        select(Relation).where(
            or_(
                and_(Relation.from_type == entity_type, Relation.from_id == ghost_id),
                and_(Relation.to_type == entity_type, Relation.to_id == ghost_id),
            )
        )
    ).all()
    for row in rows:
        new_from = (
            survivor_id
            if row.from_type == entity_type and row.from_id == ghost_id
            else row.from_id
        )
        new_to = (
            survivor_id
            if row.to_type == entity_type and row.to_id == ghost_id
            else row.to_id
        )
        if row.from_type == row.to_type and new_from == new_to:
            session.delete(row)
            continue
        duplicate = session.scalar(
            select(Relation).where(
                Relation.from_type == row.from_type,
                Relation.from_id == new_from,
                Relation.to_type == row.to_type,
                Relation.to_id == new_to,
                Relation.kind == row.kind,
                Relation.id != row.id,
            )
        )
        if duplicate is not None:
            session.delete(row)
            continue
        row.from_id, row.to_id = new_from, new_to
    session.flush()


def _ensure_relation(
    session: Session,
    *,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    kind: str,
    provenance: dict,
) -> None:
    existing = session.scalar(
        select(Relation).where(
            Relation.from_type == from_type,
            Relation.from_id == from_id,
            Relation.to_type == to_type,
            Relation.to_id == to_id,
            Relation.kind == kind,
        )
    )
    if existing is None:
        session.add(
            Relation(
                from_type=from_type,
                from_id=from_id,
                to_type=to_type,
                to_id=to_id,
                kind=kind,
                provenance=provenance,
            )
        )


def _knowledge_entities(
    session: Session, state: ProcessingState
) -> tuple[SourceObject, Document, DocumentVersion]:
    source_object = _source_object_with_hash(session, state)
    link = session.scalar(
        select(DocumentVersionSource).where(
            DocumentVersionSource.source_object_id == source_object.id
        )
    )
    version = session.get(DocumentVersion, link.version_id) if link else None
    document = session.get(Document, version.document_id) if version else None
    if document is None or version is None:
        raise RetryableStageError("knowledge entities are missing")
    return source_object, document, version


def split_text(text: str, *, size: int, overlap: int) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + size)
        if end < len(normalized):
            boundary = normalized.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        chunks.append(normalized[start:end].strip())
        if end == len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks
