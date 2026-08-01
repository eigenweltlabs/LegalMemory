"""Restoring as something an operator can do from a screen, on the same ledger as the rest.

Restoring used to live only in ``ki backup-restore`` and ``scripts/restore-backup.sh``. The
argument for that was honest — applying a restore destroys the running appliance, and half
of it needs containers stopped — but the conclusion was wrong. The operation that has to
work is the one performed by somebody who has never performed it, on the worst day the firm
has had, and telling that person to find a terminal and compose a command with six flags is
where a recovery stops.

So the safe half is a button. Staging downloads, decrypts and re-checks every component
against the manifest and writes nothing outside its own directory; it is the operation that
turns "we have backups" into "we have restorable backups", and it is exactly the thing a
firm should be doing on a quiet Tuesday rather than discovering during an outage. It runs
here, on the ``pipeline_runs`` ledger, with progress, because on a real estate it is minutes
to hours and no HTTP request should hold that open.

Applying is behind the same ledger and an explicit per-store confirmation. Two stores are
still not applyable from here and say so: replacing Keycloak's data volume or Hatchet's
config means stopping those containers, and a process inside the stack cannot stop the
stack it runs in.

The other thing this adds is restoring from somewhere that is not the configured
destination. A recovery onto fresh hardware starts with a NAS mounted somewhere and an
appliance that has never heard of it, and a restore that can only read the destination in
``config.json`` cannot help. An override folder is checked against the same roots the
folder picker offers, so an admin-only endpoint cannot be pointed at arbitrary paths.

Applying is also not finished when the last store is written. ``pg_restore --clean
--if-exists`` drops and recreates every type and table under services that are still
connected, and those services keep using cached query plans and type OIDs for objects that
have gone: Hatchet stops polling its cron schedules and acquiring queue leases, the worker
loses its registration and repeats "invalid auth token" until somebody intervenes. So a
restore ends by restarting exactly the services whose store it replaced — derived from what
was applied, so restoring a folder of documents does not bounce the orchestrator — and
says, on the run, which ones it could not do and what to type.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.backup import restore as restore_module
from knowledge_index.backup import runs as backup_runs
from knowledge_index.backup.destinations import build_destination
from knowledge_index.config import AppConfig
from knowledge_index.db.models import PipelineRun as PipelineRunRecord

WORKFLOW = "restore"
ACTIVE_STATUSES = ("queued", "running")

# Where a staged restore lands. Inside the appliance's own data directory rather than /tmp,
# because a staged backup is the whole estate in plaintext and it must not sit on a volume
# that a restart empties halfway through a recovery. Deployment layout rather than policy,
# so it is an environment variable next to the backup staging directory rather than a
# setting an administrator has to keep correct in two places.
STAGE_DIR_ENV = "KI_RESTORE_STAGE_DIR"
DEFAULT_STAGE_ROOT = "/data/restore"

# Which compose services are left broken by restoring each store, and therefore have to be
# restarted once it is back. Keyed on the component so the set is derived from what was
# actually applied: a restore of nothing but the document blobs must not bounce Hatchet,
# LiteLLM and Langfuse for databases none of them noticed being touched.
_SERVICES_BY_COMPONENT: dict[str, tuple[str, ...]] = {
    # Three processes hold a pool against the appliance's own database, and all three go on
    # using plans and type OIDs that name tables pg_restore dropped.
    "postgres/ki": ("app", "watcher", "worker"),
    # The orchestrator's database holds the tokens it issued and the workers registered
    # against them, so replacing it invalidates the connected worker as surely as Hatchet
    # itself — observed as the worker repeating gRPC UNAUTHENTICATED "invalid auth token"
    # and NOT_FOUND "worker not found" until it was restarted by hand.
    "postgres/hatchet": ("hatchet", "worker"),
    # Replacing this volume already restarts Hatchet, because the agent has to stop the
    # container that owns it. What that does not fix is the worker: the config volume is
    # what makes an issued token valid, so every token minted under the old one is refused
    # from the moment the new one is in place.
    "volumes/hatchet-config": ("worker",),
    "postgres/litellm": ("litellm",),
    "postgres/langfuse": ("langfuse",),
}

# Restarted in this order: what the appliance's own processes depend on first, so they come
# back to a live orchestrator and a live gateway rather than reconnecting to nothing, and
# the worker last because it is usually the process running the restore and therefore the
# one that has to be left to an operator.
_RESTART_ORDER = ("hatchet", "litellm", "langfuse", "watcher", "app", "worker")


def stage_root() -> Path:
    return Path(os.environ.get(STAGE_DIR_ENV, DEFAULT_STAGE_ROOT)).expanduser()


def _log(message: str) -> None:
    print(f"[ki restore] {message}", file=sys.stderr, flush=True)


class RestoreNotAllowed(RuntimeError):
    """The restore was refused before anything was read or written."""


@dataclass(frozen=True)
class EnqueuedRestore:
    run_id: str
    backup_id: str

    def payload(self) -> dict:
        return {"run_id": self.run_id, "backup_id": self.backup_id}


# --------------------------------------------------------------- reading somewhere else


def destination_for(
    config: AppConfig, source_path: str | None, session_factory: sessionmaker[Session] | None
):
    """The destination to read a backup from: the configured one, or a folder given here.

    The override keeps the configured *kind* and swaps only the location. A firm restoring
    onto fresh hardware has the same sort of destination it always had — a directory, or a
    restic repository in one — mounted somewhere new, and asking them to also re-describe
    what kind of thing it is invites getting it wrong at the worst moment.
    """
    secrets = backup_runs.destination_secrets(session_factory)
    if not (source_path or "").strip():
        return build_destination(config.backup.destination, secrets)

    from knowledge_index.backup import browse

    folder = Path(source_path.strip()).expanduser()
    # Same guard as the folder picker: an admin-only endpoint still must not be a way to
    # point the appliance at any path on the host and have it read what is there.
    if not browse._within_roots(folder.resolve(), config.backup.destination.path):
        raise RestoreNotAllowed(
            f"{folder} is not one of the folders this appliance offers for backups. Mount "
            "the drive holding the backup into the container and it will appear here."
        )
    if not folder.is_dir():
        raise RestoreNotAllowed(f"{folder} is not a folder this appliance can see")
    override = config.backup.destination.model_copy(
        update={"path": str(folder), "prefix": "", "restic_repository": ""}
    )
    return build_destination(override, secrets)


def list_backups_at(
    config: AppConfig,
    source_path: str | None,
    session_factory: sessionmaker[Session] | None = None,
    *,
    limit: int = 100,
) -> list[dict]:
    """Every complete backup at a folder, described the way the backup list describes them."""
    destination = destination_for(config, source_path, session_factory)
    entries: list[dict] = []
    for backup_id in sorted(destination.list_backups(), reverse=True)[:limit]:
        try:
            manifest = backup_runs.load_manifest(
                config, backup_id, destination=destination, session_factory=session_factory
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable backup is a listed problem
            entries.append(
                {
                    "backup_id": backup_id,
                    "complete": False,
                    "problem": str(exc),
                    "components": [],
                    "warnings": [],
                }
            )
            continue
        summary = manifest.summary()
        summary["complete"] = True
        summary["problem"] = None
        entries.append(summary)
    return entries


# ------------------------------------------------------------------------- reservation


def enqueue_restore(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    backup_id: str,
    source_path: str | None = None,
    apply_databases: bool = False,
    apply_files: bool = False,
    apply_search_index: bool = False,
    apply_volumes: bool = False,
    trigger: str = "api",
) -> EnqueuedRestore:
    """Reserve one restore run and hand it to the orchestrator.

    The plan is evaluated here, before anything is reserved, so a restore that cannot work
    is refused while the appliance is still the way it was — a mismatched connector
    credential key is the one that matters, because restoring under the wrong one leaves
    every stored token undecryptable and the estate quietly stops syncing a week later.
    """
    applying = apply_databases or apply_files or apply_search_index or apply_volumes
    plan = restore_module.restore_plan(config, backup_id, session_factory, source_path=source_path)
    if plan["blockers"]:
        raise RestoreNotAllowed("; ".join(plan["blockers"]))

    with session_factory() as session:
        backup_runs._advisory_xact_lock(session, "backup:appliance")
        # Under the same lock, for the same reason a reservation does it: a restore brings
        # back the ledger as it was mid-backup, so the archive's own capture returns as a
        # run in flight that nothing will finish. It refuses restores exactly as it refuses
        # backups, and an operator who has just restored the wrong backup is the last person
        # who should be told to wait for a run that does not exist.
        backup_runs.release_restored_runs(session)
        active = session.scalar(
            select(PipelineRunRecord.id)
            .where(
                PipelineRunRecord.workflow.in_((WORKFLOW, backup_runs.WORKFLOW)),
                PipelineRunRecord.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if active is not None:
            raise RestoreNotAllowed(
                f"a backup or restore is already in flight (run {active}). Wait for it to "
                "finish, or cancel it from the pipeline page if its worker is gone."
            )
        record = PipelineRunRecord(
            provider=config.components.orchestrator_provider,
            workflow=WORKFLOW,
            status="queued",
            progress=0,
            current_step="queued",
            counters={
                "trigger": trigger,
                "backup_id": backup_id,
                "source_path": source_path or "",
                "applying": applying,
                "apply_databases": apply_databases,
                "apply_files": apply_files,
                "apply_search_index": apply_search_index,
                "apply_volumes": apply_volumes,
                "staged": 0,
                "components_planned": len(plan["steps"]),
                "warnings": list(plan["warnings"]),
                "applied": [],
            },
        )
        session.add(record)
        session.commit()
        run_id = record.id

    _dispatch(session_factory, config, run_id)
    return EnqueuedRestore(run_id=run_id, backup_id=backup_id)


def _dispatch(session_factory: sessionmaker[Session], config: AppConfig, run_id: str) -> None:
    if config.components.orchestrator_provider == "hatchet":
        from knowledge_index.orchestration.hatchet import trigger_restore

        try:
            provider_run_id = trigger_restore(session_factory, config, run_id)
        except Exception as exc:
            backup_runs._fail_run(session_factory, run_id, exc, step="dispatch")
            _log(f"run {run_id}: hatchet dispatch failed: {type(exc).__name__}: {exc}")
            raise RestoreNotAllowed(f"could not dispatch the restore: {exc}") from exc
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is not None:
                record.provider_run_id = provider_run_id
                session.commit()
        return
    from knowledge_index.sync.runs import _submit_local

    _submit_local(lambda: execute_restore_run(session_factory, config, run_id))


# --------------------------------------------------------------------------- execution


def execute_restore_run(
    session_factory: sessionmaker[Session], config: AppConfig, run_id: str
) -> dict:
    """Stage a backup, verify it, and apply only the stores that were asked for."""
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise RestoreNotAllowed(f"restore run does not exist: {run_id}")
        if record.status not in ACTIVE_STATUSES:
            raise RestoreNotAllowed(f"restore run {run_id} is already {record.status}")
        counters = dict(record.counters or {})
        record.status = "running"
        record.current_step = "preparing"
        record.started_at = record.started_at or datetime.now(UTC)
        session.commit()

    backup_id = str(counters.get("backup_id") or "")
    source_path = str(counters.get("source_path") or "") or None
    publish = backup_runs._progress_writer(session_factory, run_id)
    target = stage_root() / backup_id
    summary: dict[str, Any] = {
        "backup_id": backup_id,
        "staged": [],
        "applied": [],
        # Filled in after the applying is over: what was restarted, and what an operator
        # still has to do themselves.
        "restarted": [],
        "warnings": [],
    }

    try:
        publish("staging and verifying", {})
        staged = restore_module.stage_backup(
            config,
            backup_id,
            target,
            session_factory=session_factory,
            reuse=True,
            source_path=source_path,
        )
        summary["staged"] = [
            {"name": item.name, "kind": item.kind, "bytes": item.bytes} for item in staged
        ]
        publish(
            "staged and verified",
            {"staged": len(staged), "components_planned": counters.get("components_planned")},
        )

        for item in staged:
            applied = None
            if item.kind == "postgres" and counters.get("apply_databases"):
                publish(f"restoring {item.name}", {})
                applied = restore_module.apply_database(config, item)
            elif item.kind == "opensearch" and counters.get("apply_search_index"):
                publish(f"restoring {item.name}", {})
                applied = restore_module.apply_search_index(config, item)
            elif item.kind == "files" and counters.get("apply_files"):
                publish(f"restoring {item.name}", {})
                applied = restore_module.apply_files(config, item)
            elif item.kind == "volume" and counters.get("apply_volumes"):
                # Last, and one at a time: each stops the container that owns it. Doing
                # them before the databases would take identity down while the longest
                # part of the restore was still running.
                publish(f"restoring {item.name} (stopping its container)", {})
                applied = restore_module.apply_volume(config, item)
            if applied is not None:
                summary["applied"].append(applied)
                # A store that came back with real errors fails the run rather than being
                # one line in a summary nobody reads. Half a restore reported as success is
                # the failure this whole feature exists to avoid.
                if applied.get("ok") is False:
                    raise restore_module.RestoreError(
                        f"{item.name} did not restore cleanly: "
                        + "; ".join(applied.get("serious_errors") or ["pg_restore reported errors"])
                    )
    except Exception as exc:
        backup_runs._fail_run(session_factory, run_id, exc, step="restore")
        _log(f"run {run_id}: restore failed: {type(exc).__name__}: {exc}")
        raise
    finally:
        # The staged copy is the estate in plaintext. Kept only when nothing was applied,
        # because that is the drill case where an operator may want to look at what came
        # back; once it has been written into the appliance it is duplicate exposure.
        if counters.get("applying"):
            shutil.rmtree(target, ignore_errors=True)

    # Everything from here on is repair, and none of it may fail the run: the estate is
    # back, and a restore reported as failed because a container did not bounce is a firm
    # restoring a second time over a database that was already correct.
    services = services_to_restart(summary["applied"])
    if services:
        # The progress line goes into the database this restore may have just replaced, so
        # it does not get to decide whether the repair happens. A pool answering "cached
        # plan must not change result type" is the exact failure the restart exists to fix,
        # and letting that answer stop the restart would leave the appliance broken in the
        # way that was just diagnosed, with nothing having been attempted.
        try:
            publish(f"restarting {', '.join(services)}", {})
        except Exception as exc:  # noqa: BLE001
            _log(f"run {run_id}: could not write the restart step: {type(exc).__name__}: {exc}")
    restarted, warnings = _restart_services(config, services)
    summary["restarted"] = restarted
    summary["warnings"].extend(warnings)

    _record_outcome(session_factory, run_id, backup_id, counters, summary, target)
    return summary


# ------------------------------------------------------------------- back on its feet


def services_to_restart(applied: list[dict]) -> list[str]:
    """The compose services left broken by the stores this run actually restored.

    Derived from the components that were applied rather than from a list of everything
    this appliance runs, because the two differ in the case that matters: a firm restoring
    yesterday's documents over today's has asked for nothing that Hatchet, LiteLLM or
    Langfuse would notice, and taking them down anyway turns a narrow recovery into an
    outage.
    """
    services: set[str] = set()
    for item in applied:
        services.update(_SERVICES_BY_COMPONENT.get(str(item.get("component") or ""), ()))
    return [service for service in _RESTART_ORDER if service in services]


def _self_service(config: AppConfig) -> str:
    """The compose service this restore is running inside, and so cannot restart.

    A restore executes on the orchestrator's worker when there is one, and in the process
    that enqueued it when the orchestrator is ``local``. Getting this wrong in the safe
    direction costs one line of instructions the operator did not need; getting it wrong
    the other way stops the run that is doing the restoring, halfway through the part where
    it writes down what it did.
    """
    return "worker" if config.components.orchestrator_provider == "hatchet" else "app"


def _restart_services(config: AppConfig, services: list[str]) -> tuple[list[str], list[str]]:
    """Restart what the restore pulled a database out from under. Never raises.

    Returns the services that were restarted, and the warnings naming the ones that were
    not, in words an operator can act on.

    ``postgres/ki`` is the case that cannot be handled by a rule, because the process
    restarting the worker is the worker. Stopping it here would kill this run at the point
    where it still has to record its own outcome, and the outcome is the only evidence the
    firm has that the restore happened — ``_record_outcome`` already has to write a fresh
    record into the ledger it just replaced, and it cannot do that from a container that
    has been asked to stop. Nor can the restart be left to Docker: compose runs these
    services ``unless-stopped``, and a container stopped through the API stays stopped.

    So the worker is named rather than bounced. Everything else that holds a pool against a
    restored database — the app the operator is watching this through, the watcher, the
    orchestrator, the gateway — is restarted here, and the one service that must be done by
    hand is written onto the run in words, with the command. That is the honest division:
    the appliance repairs what it can reach and refuses to pretend about the rest.
    """
    from knowledge_index.backup import volume_agent

    if not services:
        return [], []
    myself = _self_service(config)
    mine = [service for service in services if service == myself]
    theirs = [service for service in services if service != myself]

    warnings: list[str] = []
    if mine:
        warnings.append(
            f"restart {' and '.join(mine)} by hand, last: `docker compose restart "
            f"{' '.join(mine)}`. The restore ran inside it, so it could not be restarted "
            "from here without killing the run doing the restoring. Until it is restarted "
            "it holds cached query plans and type identifiers for tables this restore "
            "dropped and recreated, and every job it takes will fail."
        )
    if theirs and not volume_agent.available():
        # Not a failure of the restore, and it must not read as one. What it is, is an
        # appliance whose data is correct and whose services are still talking to the
        # database as it was ten minutes ago, so the operator gets the list and the command.
        warnings.append(
            "the restore agent could not be reached, so nothing was restarted. These "
            "services are still connected to a database this restore replaced and will "
            f"keep failing until somebody restarts them: {', '.join(theirs)}. Run: `docker "
            f"compose restart {' '.join(theirs)}`."
        )
        return [], warnings

    restarted: list[str] = []
    for service in theirs:
        try:
            volume_agent.restart_service(service)
        # A restart that did not happen is reported to the operator, never raised: the
        # restore itself is already done and correct.
        except Exception as exc:  # noqa: BLE001
            _log(f"could not restart {service}: {type(exc).__name__}: {exc}")
            warnings.append(
                f"{service} could not be restarted ({exc}). It is still connected to a "
                f"database this restore replaced; run `docker compose restart {service}`."
            )
        else:
            _log(f"restarted {service} after restoring the database it was using")
            restarted.append(service)
    return restarted, warnings


def _record_outcome(
    session_factory: sessionmaker[Session],
    run_id: str,
    backup_id: str,
    counters: dict,
    summary: dict,
    target: Path,
) -> None:
    """Close out the run — or write a fresh record of it, if it restored its own ledger.

    Restoring ``postgres/ki`` replaces every table in the appliance's database, and
    ``pipeline_runs`` is one of them. The row tracking this restore was written after the
    dump was taken, so applying that dump deletes it while it is still being used. That is
    not a fault and cannot be avoided: the run is destroyed by the very thing it is doing.

    What must not happen is the restore then appearing never to have taken place. So when
    the row has gone, a completed one is written into the restored ledger instead — the
    firm ends up with a record, in the database they now have, saying that this database
    came from that backup at that moment.
    """
    pending = list(summary.get("warnings") or [])
    outcome = {
        "backup_id": backup_id,
        "components_captured": len(summary["staged"]),
        "components_planned": counters.get("components_planned"),
        "bytes_stored": 0,
        "bytes_plaintext": sum(item["bytes"] for item in summary["staged"]),
        "warnings": list(counters.get("warnings") or []) + pending,
        "verified": {"ok": True},
        "pruned": [],
        "seconds": 0,
    }
    applying = bool(counters.get("applying"))
    step = "restored" if applying else "staged and verified"
    extra = {
        "applied": summary["applied"],
        "restarted": list(summary.get("restarted") or []),
        "staged": len(summary["staged"]),
        "staged_to": "" if applying else str(target),
        "trigger": counters.get("trigger"),
        "applying": applying,
        "source_path": counters.get("source_path") or "",
    }
    # A run row has exactly one field whose text the ledger prints for an operator to read,
    # and it is the error slot. A restore that put the estate back but left the worker
    # holding dead type identifiers is an appliance that looks restored and does no work,
    # and telling that operator inside a counters list nothing renders is the same as not
    # telling them. So the instructions go where they will be seen, on a run whose status
    # stays "completed" because the restore did succeed — every store asked for was written
    # back, and what is left is one command at a terminal.
    attention = (
        {"class": "RestartRequired", "message": " ".join(pending), "detected_by": "restore"}
        if pending
        else None
    )
    if attention:
        step = "restored — needs a restart"

    with session_factory() as session:
        if session.get(PipelineRunRecord, run_id) is None:
            session.add(
                PipelineRunRecord(
                    id=run_id,
                    workflow=WORKFLOW,
                    status="completed",
                    progress=1,
                    current_step=step,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    last_error=attention,
                    counters={
                        **extra,
                        **outcome,
                        "ledger_replaced": True,
                    },
                )
            )
            session.commit()
            _log(
                f"run {run_id}: the restored database replaced the run ledger, so this "
                "restore has been recorded in the ledger it restored"
            )
            return

    backup_runs._complete_run(session_factory, run_id, outcome)
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is not None:
            record.counters = {**dict(record.counters or {}), **extra}
            record.current_step = step
            record.last_error = attention
            session.commit()


__all__ = [
    "WORKFLOW",
    "EnqueuedRestore",
    "RestoreNotAllowed",
    "destination_for",
    "enqueue_restore",
    "execute_restore_run",
    "list_backups_at",
    "services_to_restart",
]
