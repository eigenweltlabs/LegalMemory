"""One way to start an insertion run, whichever orchestrator is configured.

The API endpoint, the reindex action, the folder watcher and the sync handoff all start
the same pipeline. Three of them used to build the ``pipeline_runs`` row and call the
orchestrator themselves, which is how the watcher's runs and the button's runs came to
differ in what they recorded. This is the single implementation they share.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import PipelineRun as PipelineRunRecord

SUPPORTED_PROVIDERS = ("local", "hatchet")


class OrchestratorUnavailable(RuntimeError):
    """The configured orchestrator refused or could not accept the run."""


def launch_insertion(session_factory: sessionmaker[Session], config: AppConfig) -> dict:
    """Start one insertion run and return its recorded identity.

    Raises ``OrchestratorUnavailable`` when the orchestrator rejects the trigger; the
    ``pipeline_runs`` row is left in ``failed`` with the cause so the failure is visible
    in the run list rather than only in a caller's stack trace.
    """
    provider = config.components.orchestrator_provider
    if provider not in SUPPORTED_PROVIDERS:
        # Fail closed: an unrecognized provider must never quietly fall back to running
        # the corpus in whatever process happened to ask.
        raise OrchestratorUnavailable(f"unknown orchestrator provider: {provider}")

    run_id = _create_run(session_factory, provider)
    if provider == "hatchet":
        from knowledge_index.orchestration.hatchet import trigger_insertion

        try:
            provider_run_id = trigger_insertion(session_factory, config, run_id)
        except Exception as exc:
            _fail_run(session_factory, run_id, exc)
            raise OrchestratorUnavailable(f"hatchet trigger failed: {exc}") from exc
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is not None:
                record.provider_run_id = provider_run_id
                session.commit()
        return {
            "run_id": run_id,
            "provider": "hatchet",
            "provider_run_id": provider_run_id,
            "status": "queued",
        }

    from knowledge_index.pipeline import PipelineRunner

    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is not None:
            record.status = "running"
            record.current_step = "claim"
            record.started_at = datetime.now(UTC)
            session.commit()
    try:
        result = PipelineRunner(session_factory, config).run_until_idle()
    except Exception as exc:
        _fail_run(session_factory, run_id, exc)
        raise
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is not None:
            record.status = "completed"
            record.progress = 1
            record.current_step = "complete"
            record.counters = result.__dict__
            record.finished_at = datetime.now(UTC)
            session.commit()
    return {"run_id": run_id, **result.__dict__}


def _create_run(session_factory: sessionmaker[Session], provider: str) -> str:
    with session_factory() as session:
        record = PipelineRunRecord(
            provider=provider,
            workflow="insertion",
            status="queued",
            progress=0,
            current_step="queued",
        )
        session.add(record)
        session.commit()
        return record.id


def _fail_run(session_factory: sessionmaker[Session], run_id: str, exc: BaseException) -> None:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            return
        record.status = "failed"
        record.last_error = {"class": type(exc).__name__, "message": str(exc)}
        record.finished_at = datetime.now(UTC)
        session.commit()
