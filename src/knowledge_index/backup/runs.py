"""A backup as an orchestrated run, on the same ledger as sync and insertion.

Backing up this appliance is minutes to hours of dumping, compressing and transferring
tens or hundreds of gigabytes. That is not something an HTTP request can hold open, and it
is not something two operators clicking at once should do twice. So a request only
*reserves* the work — one ``pipeline_runs`` row with ``workflow = 'backup'`` — and
everything after that is recorded on the row that ``/api/runs`` already serves. The
existing run sweeper resolves a backup whose worker died exactly as it resolves a sync.

The order things happen in is the part worth reading twice:

1. reserve the run, so a second request is refused rather than queued;
2. prove the destination is writable *before* anything is dumped, because discovering an
   unmounted share after a two-hour pg_dump wastes the two hours and, worse, teaches an
   operator that failures are normal;
3. capture, transfer and delete one component at a time, so the staging disk needs room
   for the largest store rather than for the whole appliance;
4. write the manifest last. A backup directory without a manifest is an incomplete backup,
   and that is precisely how listing and restore identify one;
5. read it all back and re-check every checksum, then prune.

Step 5 is not optional decoration. "Three copies, two media, one off-site, **zero
unverified restores**" is the whole of the modern form of the rule, and a backup nobody
has read back is a copy whose only tested property is that writing it did not raise.

One consequence of step 3 is worth saying out loud, because it bit a real recovery: the
dump of this appliance's own database is taken while this run is inside it, reading
``running``. Restoring that dump hands the ledger a backup in flight that nothing will
ever finish, and since a reservation refuses while one is in flight, that single row
blocks every backup and every restore afterwards. :func:`release_restored_runs` is what
recognises it, and the staging directory is what makes recognising it safe.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.backup import components as component_module
from knowledge_index.backup import secrets as backup_secrets
from knowledge_index.backup.crypto import (
    BackupCryptoError,
    EncryptingReader,
    decrypt_stream,
    key_fingerprint,
    load_key,
)
from knowledge_index.backup.destinations import Destination, DestinationError, build_destination
from knowledge_index.backup.manifest import (
    CHECKSUM_NAME,
    COMPONENT_PREFIX,
    MANIFEST_NAME,
    ComponentRecord,
    Manifest,
    ManifestError,
    checksum_file,
    new_backup_id,
)
from knowledge_index.backup.retention import plan_retention, summarize
from knowledge_index.config import AppConfig
from knowledge_index.db.models import PipelineRun as PipelineRunRecord, ProcessingState

WORKFLOW = "backup"
ACTIVE_STATUSES = ("queued", "running")
_UNSETTLED_STATES = ("pending", "running", "failed")
_VERIFY_CHUNK = 1024 * 1024

# The counter a capturing backup uses to name the staging directory it is holding. It is
# set only while the directory is there and given back before it goes, never after, which
# is what lets a reservation tell a backup that is running here from a copy of one that a
# restore put back on the ledger — see :func:`release_restored_runs`.
STAGING_COUNTER = "staging_dir"


def destination_secrets(session_factory: sessionmaker[Session] | None = None) -> dict:
    """Every backup secret, resolved once.

    One place, so no call site can report a key as missing that an administrator set in
    the admin UI ten seconds earlier. The session factory is passed in rather than built
    here: a module that quietly opens its own engine leaves a connection pool behind in
    every process that imports it, which is a side effect nothing asked for.
    """
    return {name: backup_secrets.resolve(name, session_factory) for name in backup_secrets.KNOWN}


def _log(message: str) -> None:
    print(f"[ki backup] {message}", file=sys.stderr, flush=True)


class BackupNotConfigured(RuntimeError):
    """Backups are switched off, or the destination is not usable."""


class BackupRunFailed(RuntimeError):
    """A reserved backup run ended in ``failed``; the cause is on the run row."""


class BackupNotFound(LookupError):
    """No backup with that id exists at the configured destination."""


@dataclass(frozen=True)
class EnqueuedBackup:
    run_id: str
    backup_id: str

    def payload(self) -> dict:
        return {"run_id": self.run_id, "backup_id": self.backup_id}


# ----------------------------------------------------------------------------- enqueue


def enqueue_backup(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    trigger: str = "api",
    force: bool = False,
) -> EnqueuedBackup:
    """Reserve one backup run and hand it to the orchestrator. Never backs up inline.

    ``force`` skips the settled-pipeline check. An operator who is about to take the
    appliance down wants a backup now, even a slightly ragged one, more than they want a
    refusal — but it has to be asked for, so the nightly schedule cannot quietly take
    inconsistent backups forever.
    """
    if not config.backup.enabled:
        raise BackupNotConfigured(
            "backups are not enabled. Turn them on under Backup in the admin UI, or set "
            "KI_BACKUP__ENABLED=true, once a destination is configured."
        )
    backup_id = new_backup_id()
    run_id = _reserve_run(session_factory, config, backup_id, trigger, force)
    _dispatch(session_factory, config, run_id)
    return EnqueuedBackup(run_id=run_id, backup_id=backup_id)


def _reserve_run(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    backup_id: str,
    trigger: str,
    force: bool,
) -> str:
    with session_factory() as session:
        # One backup at a time for the whole appliance. Unlike sync there is no per-source
        # partial unique index to fall back on — a backup has no source — so this advisory
        # lock plus the active-run check is the whole of the mutual exclusion, and both
        # halves have to stay inside one transaction.
        _advisory_xact_lock(session, "backup:appliance")
        # Under the same lock and in the same transaction as the check below, so nothing
        # can slip in between a ghost being released and the ledger being seen empty.
        release_restored_runs(session)
        active = session.scalar(
            select(PipelineRunRecord.id)
            .where(
                PipelineRunRecord.workflow == WORKFLOW,
                PipelineRunRecord.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if active is not None:
            raise BackupNotConfigured(
                f"a backup is already in flight (run {active}). Wait for it to finish, or "
                "cancel it from the pipeline page if its worker is gone."
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
                "force": force,
                "destination": config.backup.destination.kind,
                "encrypted": config.backup.encrypt,
                "components_captured": 0,
                "components_planned": None,
                "bytes_stored": 0,
                "warnings": [],
                "verified": None,
                "pruned": [],
            },
        )
        session.add(record)
        session.commit()
        return record.id


def release_restored_runs(session: Session) -> list[str]:
    """Fail the backups a restore put back on the ledger. Joins the caller's transaction.

    A backup dumps ``postgres/ki`` while its own run row reads ``running``, so every
    backup archive contains a ledger with a backup in flight in it — its own. Restoring
    that ledger brings the row back as a live run no worker has ever heard of, and because
    the appliance allows one backup at a time, that row then refuses every later backup and
    every later restore. On a real recovery it is the first thing an operator meets after
    the estate comes back, and the only way out was to delete the row by hand.

    The staging directory is what makes releasing it safe rather than merely convenient.
    :func:`perform_backup` records the directory on the run row immediately after creating
    it and gives it back immediately *before* deleting it, so the counter is never set for
    an instant longer than the directory is there — and the capture is the only part of a
    backup during which a dump of this database can be taken, because that dump is one of
    the components being captured. Two things follow, and together they are the whole
    argument:

    * the only active backup row a dump can contain is the dumping run itself, since a
      second one could never have been reserved while it held the reservation, and at that
      instant it was holding the directory the row names;
    * that directory is deleted before the run ends, so a row that still names one either
      is holding it now or was written somewhere this appliance's disk cannot corroborate.

    So an active backup naming a staging directory that is not on the disk is not a backup
    this appliance is running. A backup that really is capturing is writing into the
    directory it names, on the ``/data`` volume every KI container shares, and is never
    released here — which is the half that matters, because two backups contending for one
    staging directory and one OpenSearch snapshot repository is the exact failure the
    one-at-a-time rule exists to prevent.

    Runs that never reached the capture carry no directory and are left alone: a
    reservation whose dispatch is still in flight looks identical to one whose worker died,
    and only the sweeper, which can ask the orchestrator, may decide between them.
    """
    released: list[str] = []
    now = datetime.now(UTC)
    candidates = session.scalars(
        select(PipelineRunRecord).where(
            PipelineRunRecord.workflow == WORKFLOW,
            PipelineRunRecord.status.in_(ACTIVE_STATUSES),
        )
    ).all()
    for record in candidates:
        held = str((record.counters or {}).get(STAGING_COUNTER) or "")
        if not held or Path(held).is_dir():
            continue
        record.status = "failed"
        record.finished_at = record.finished_at or now
        # current_step is left where the capture stopped, as the run sweeper leaves it:
        # "capturing postgres/ki" is the line that explains this row to an operator.
        record.last_error = {
            "class": "RestoredRun",
            "message": (
                f"this backup was captured into an archive while it was running, and came "
                f"back on a ledger restored from that archive. Its staging directory "
                f"({held}) is not on this appliance, so no backup here is using it."
            ),
            "detected_by": "backup-reservation",
        }
        released.append(record.id)
        _log(
            f"run {record.id}: released — it came back from a restore rather than from a "
            f"worker here, and the staging directory it claims ({held}) does not exist"
        )
    if released:
        # Flushed rather than left to autoflush: the caller's very next statement is the
        # check for a backup in flight, and it must not find the rows just released.
        session.flush()
    return released


def _dispatch(session_factory: sessionmaker[Session], config: AppConfig, run_id: str) -> None:
    if config.components.orchestrator_provider == "hatchet":
        from knowledge_index.orchestration.hatchet import trigger_backup

        try:
            provider_run_id = trigger_backup(session_factory, config, run_id)
        except Exception as exc:
            # The reservation is durable, so a trigger that never landed has to be closed
            # out here or every later backup is refused by a run nobody will finish.
            _fail_run(session_factory, run_id, exc, step="dispatch")
            _log(f"run {run_id}: hatchet dispatch failed: {type(exc).__name__}: {exc}")
            raise BackupRunFailed(f"could not dispatch the backup: {exc}") from exc
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is not None:
                record.provider_run_id = provider_run_id
                session.commit()
        return
    from knowledge_index.sync.runs import _submit_local

    _submit_local(lambda: execute_backup_run(session_factory, config, run_id))


def wait_for_run(
    session_factory: sessionmaker[Session], run_id: str, *, timeout: float = 86400.0
) -> dict:
    """Poll one backup run to a terminal state, for a caller who is a human at a terminal."""
    deadline = time.monotonic() + timeout
    while True:
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is None:
                raise BackupRunFailed(f"backup run disappeared: {run_id}")
            snapshot = {
                "run_id": record.id,
                "status": record.status,
                "current_step": record.current_step,
                "counters": dict(record.counters or {}),
                "error": record.last_error,
            }
        if snapshot["status"] in ("completed", "failed"):
            return snapshot
        if time.monotonic() > deadline:
            raise TimeoutError(f"backup run {run_id} did not finish within {timeout}s")
        time.sleep(1.0)


# --------------------------------------------------------------------------- execution


def execute_backup_run(
    session_factory: sessionmaker[Session], config: AppConfig, run_id: str
) -> dict:
    """Perform one reserved backup, recording every step on its run row."""
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            raise BackupRunFailed(f"backup run does not exist: {run_id}")
        if record.status not in ACTIVE_STATUSES:
            raise BackupRunFailed(f"backup run {run_id} is already {record.status}")
        counters = dict(record.counters or {})
        backup_id = str(counters.get("backup_id") or new_backup_id())
        force = bool(counters.get("force"))
        record.status = "running"
        record.current_step = "preparing"
        record.started_at = record.started_at or datetime.now(UTC)
        session.commit()

    publish = _progress_writer(session_factory, run_id)
    try:
        if config.backup.require_settled_pipeline and not force:
            unsettled = count_unsettled(session_factory)
            if unsettled:
                raise BackupRunFailed(
                    f"{unsettled} document(s) are still mid-pipeline. A backup taken now "
                    "would hold a database that knows about files the artifact archive has "
                    "not finished writing. Wait for the pipeline to settle, or run the "
                    "backup with force to accept that."
                )
        summary = perform_backup(
            config, backup_id, publish=publish, session_factory=session_factory
        )
    except Exception as exc:
        _fail_run(session_factory, run_id, exc, step="backup")
        _log(f"run {run_id}: backup failed: {type(exc).__name__}: {exc}")
        raise
    _complete_run(session_factory, run_id, summary)
    return summary


def perform_backup(
    config: AppConfig,
    backup_id: str,
    *,
    publish: Callable[[str, dict], None] | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> dict:
    """Capture, transfer, describe and verify one backup. The run row is optional here.

    Split out from :func:`execute_backup_run` so the CLI can take a backup on a machine
    that is not carrying a run ledger, and so tests can exercise the whole path without an
    orchestrator.
    """
    emit = publish or (lambda _step, _counters: None)
    destination = build_destination(
        config.backup.destination, destination_secrets(session_factory)
    )
    emit("checking the destination", {})
    destination.check_writable()

    # Who encrypts, and therefore whether anything here compresses. A destination that
    # encrypts for itself must be handed plaintext: sealing first would give it a stream
    # that shares no chunks with last night's and deduplication — the only reason to use
    # such a destination — would silently stop working while still reporting success.
    destination_encrypts = bool(getattr(destination, "provides_encryption", False))
    key = (
        load_key(backup_secrets.resolve(backup_secrets.ENCRYPTION_KEY, session_factory))
        if config.backup.encrypt and not destination_encrypts
        else None
    )
    compress = not bool(getattr(destination, "prefers_uncompressed", False))
    # Only the components that will actually be captured. Counting the ones the plan
    # already knows are unreachable puts them in the denominator of a fraction nothing can
    # ever add to, so a healthy backup of a stack without Langfuse would stall at four
    # fifths and look stuck.
    planned = [item for item in component_module.plan(config) if item.enabled and item.ready]
    manifest = Manifest(
        backup_id=backup_id,
        created_at=datetime.now(UTC).isoformat(),
        appliance=_appliance_metadata(config, session_factory),
        encryption=_encryption_record(config, key, destination_encrypts),
        config_digest=_config_digest(config),
    )
    # Only the concerns the capture itself cannot report. Every store that is configured
    # but unreachable produces its own warning as it is skipped below, and seeding those
    # from the plan as well gave every backup two lines saying the same thing.
    want_secrets = config.backup.sources.environment_secrets
    if want_secrets and "KI_CONNECTOR_CREDENTIAL_KEY" not in os.environ:
        manifest.warnings.append(
            "secrets/environment: KI_CONNECTOR_CREDENTIAL_KEY is not set in this container, "
            "so the one secret a restore cannot do without is not in this backup"
        )

    staging = component_module.prepare_staging(backup_id)
    # Claimed on the run row before the first component is captured, and given back below.
    # The dump of this appliance's own database happens inside the loop, so this is what a
    # copy of this row carries when it comes back on a restored ledger; see
    # release_restored_runs for why an absent directory then proves the copy is not live.
    emit("", {STAGING_COUNTER: str(staging)})
    limit_bytes = config.backup.max_component_gb * 1024**3
    captured = 0
    stored_total = 0
    started = time.monotonic()
    try:
        for component, warning in component_module.collect(
            config,
            staging,
            backup_id,
            on_step=lambda message: emit(
                message, {"components_captured": captured, "components_planned": len(planned)}
            ),
            compress=compress,
        ):
            if component is None:
                if warning:
                    manifest.warnings.append(warning)
                    _log(warning)
                continue
            if component.plaintext_bytes > limit_bytes:
                raise BackupRunFailed(
                    f"{component.name} is {component.plaintext_bytes / 1024**3:.1f} GB, over "
                    f"the {config.backup.max_component_gb} GB per-component limit. Raise "
                    "backup.max_component_gb if this store really is that large."
                )
            record = _transfer(destination, backup_id, component, key)
            manifest.components.append(record)
            captured += 1
            stored_total += record.stored_bytes
            # Deleted as soon as it is at the destination: the staging disk only ever has
            # to hold the largest single component, not the sum of them.
            component.path.unlink(missing_ok=True)
            emit(
                f"stored {component.name}",
                {
                    "components_captured": captured,
                    "components_planned": len(planned),
                    "bytes_stored": stored_total,
                    "warnings": list(manifest.warnings),
                },
            )
    finally:
        # Given back before the directory goes, never after, and the asymmetry is the whole
        # of the safety argument. These are two writes, and between them the row says
        # something untrue either way; only one of the two untruths is survivable. A row
        # still naming a directory it has already deleted is indistinguishable from a
        # restored ghost, so a reservation landing in that gap — the nightly schedule, an
        # operator clicking Back up — fails a run that is about to write its manifest and
        # verify, and starts a second backup against the same destination and the same
        # snapshot repository. A row that has given the directory back while it is still on
        # disk merely refuses one more backup until the rmtree finishes. The gap is one
        # pooled session and one commit wide, which is milliseconds until the worker's pool
        # is saturated and then it is as long as KI_DATABASE_POOL_TIMEOUT_SECONDS allows.
        try:
            emit("", {STAGING_COUNTER: ""})
        finally:
            # Deleted even if that write raised. For the length of a run this directory is
            # every store on the appliance in the clear, and the only other thing that ever
            # removes it is the next backup to be given the same id — which is to say
            # nothing.
            component_module.clear_staging(staging)

    emit("writing the manifest", {"components_captured": captured})
    _write_manifest(destination, backup_id, manifest)

    verified: dict | None = None
    if config.backup.verify_after_write:
        emit("verifying", {"components_captured": captured})
        verified = verify_backup(
            config, backup_id, destination=destination, session_factory=session_factory
        )
        if not verified["ok"]:
            raise BackupRunFailed(
                f"backup {backup_id} was written but failed verification: "
                f"{'; '.join(verified['problems'])}. Treat it as unusable."
            )

    pruned: dict | None = None
    if config.backup.retention.prune_enabled:
        emit("applying retention", {"components_captured": captured})
        pruned = prune_backups(
            config,
            destination=destination,
            keep_id=backup_id,
            session_factory=session_factory,
        )

    return {
        "backup_id": backup_id,
        "seconds": round(time.monotonic() - started, 1),
        "components_captured": captured,
        "components_planned": len(planned),
        "bytes_stored": stored_total,
        "bytes_plaintext": manifest.total_plaintext_bytes,
        "encrypted": bool(key),
        "warnings": list(manifest.warnings),
        "verified": verified,
        "pruned": (pruned or {}).get("pruned", []),
        "destination": destination.describe(),
        "manifest": manifest.summary(),
    }


def _transfer(
    destination: Destination, backup_id: str, component: component_module.StagedComponent, key
) -> ComponentRecord:
    stored_key = f"{COMPONENT_PREFIX}/{component.filename}"
    with component.path.open("rb") as handle:
        reader = (
            EncryptingReader(handle, key, context=_context(backup_id, component.name))
            if key
            else handle
        )
        stored = destination.write(backup_id, stored_key, reader)
    return ComponentRecord(
        name=component.name,
        kind=component.kind,
        key=stored_key,
        plaintext_bytes=component.plaintext_bytes,
        plaintext_sha256=component.sha256,
        stored_bytes=stored.bytes_written,
        stored_sha256=stored.sha256,
        encrypted=bool(key),
        detail=component.detail,
    )


def _encryption_record(config: AppConfig, key, destination_encrypts: bool) -> dict | None:
    """What the manifest says about how this backup is protected.

    A manifest that reads ``"encryption": null`` because restic did the encrypting would
    tell an operator the opposite of the truth at exactly the wrong moment, so the
    destination's encryption is recorded as its own kind rather than left as an absence.
    """
    if key:
        return {
            "algorithm": "AES-256-GCM",
            "key_fingerprint": key_fingerprint(key),
            "performed_by": "appliance",
        }
    if destination_encrypts:
        return {
            "algorithm": "restic repository",
            "performed_by": "destination",
        }
    return None


def _context(backup_id: str, component_name: str) -> dict:
    """What a sealed component claims to be, authenticated by every one of its chunks.

    The manifest and SHA256SUMS are not encrypted and not signed, so anyone who can write
    to the destination can rewrite them. This is what stops that being enough to slip last
    month's database dump into tonight's backup: the substituted component's own tags say
    which backup and which component it was sealed as, and they cannot be recomputed
    without the key.
    """
    return {"backup_id": backup_id, "component": component_name}


def _write_manifest(destination: Destination, backup_id: str, manifest: Manifest) -> None:
    """Write the manifest, then the checksum listing that covers it.

    In that order, and both last. A directory with components but no manifest is what an
    interrupted run leaves behind, and listing treats it as incomplete rather than
    offering an operator a backup that is missing whatever the run had not reached yet.
    """
    payload = manifest.to_json().encode("utf-8")
    manifest_sha = hashlib.sha256(payload).hexdigest()
    destination.write(backup_id, MANIFEST_NAME, io.BytesIO(payload))
    listing = checksum_file(manifest.components, manifest_sha).encode("utf-8")
    destination.write(backup_id, CHECKSUM_NAME, io.BytesIO(listing))


# ------------------------------------------------------------------- listing and verify


def load_manifest(
    config: AppConfig,
    backup_id: str,
    *,
    destination: Destination | None = None,
    session_factory: sessionmaker[Session] | None = None,
):
    target = destination or build_destination(
        config.backup.destination, destination_secrets(session_factory)
    )
    try:
        with target.open(backup_id, MANIFEST_NAME) as handle:
            return Manifest.from_json(handle.read())
    except DestinationError as exc:
        raise BackupNotFound(
            f"backup {backup_id} has no manifest at the configured destination — it is "
            f"either incomplete or not a backup ({exc})"
        ) from exc


def list_backups(
    config: AppConfig,
    *,
    limit: int = 100,
    session_factory: sessionmaker[Session] | None = None,
) -> list[dict]:
    """Every complete backup at the destination, newest first.

    Read from the manifests rather than from an index file this appliance maintains. An
    index is one more thing that can disagree with reality, and the reality that matters
    during a recovery is what is actually in the bucket.
    """
    destination = build_destination(
        config.backup.destination, destination_secrets(session_factory)
    )
    entries: list[dict] = []
    for backup_id in sorted(destination.list_backups(), reverse=True)[:limit]:
        try:
            manifest = load_manifest(
                config, backup_id, destination=destination, session_factory=session_factory
            )
        except (BackupNotFound, ManifestError) as exc:
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


def verify_backup(
    config: AppConfig,
    backup_id: str,
    *,
    destination: Destination | None = None,
    deep: bool = True,
    session_factory: sessionmaker[Session] | None = None,
) -> dict:
    """Read a backup back and check that it is what the manifest says it is.

    Two levels, and the difference matters. The stored checksum proves the bytes at the
    destination are the bytes that were written — it catches a truncated upload, a bad
    disk, a partial sync to a second NAS. The plaintext checksum, which requires actually
    decrypting, proves the key on hand opens this backup and that what comes out is the
    dump that went in. Only the second one answers "could we restore from this", so it is
    the default.
    """
    target = destination or build_destination(
        config.backup.destination, destination_secrets(session_factory)
    )
    manifest = load_manifest(
        config, backup_id, destination=target, session_factory=session_factory
    )
    key = None
    if any(item.encrypted for item in manifest.components):
        try:
            key = load_key(
                backup_secrets.resolve(backup_secrets.ENCRYPTION_KEY, session_factory)
            )
        except BackupCryptoError as exc:
            return {
                "backup_id": backup_id,
                "ok": False,
                "checked": 0,
                "problems": [str(exc)],
                "components": [],
            }
        recorded = (manifest.encryption or {}).get("key_fingerprint")
        if recorded and recorded != key_fingerprint(key):
            return {
                "backup_id": backup_id,
                "ok": False,
                "checked": 0,
                "problems": [
                    f"this backup was encrypted under key {recorded}, but the key set on "
                    f"this appliance is {key_fingerprint(key)}"
                ],
                "components": [],
            }

    problems: list[str] = []
    results: list[dict] = []
    for component in manifest.components:
        component_key = key if component.encrypted else None
        outcome = _verify_component(target, backup_id, component, component_key, deep)
        results.append(outcome)
        problems.extend(outcome["problems"])
    return {
        "backup_id": backup_id,
        "ok": not problems,
        "checked": len(results),
        "deep": deep,
        "problems": problems,
        "components": results,
        "warnings": list(manifest.warnings),
    }


def _verify_component(
    destination: Destination, backup_id: str, component: ComponentRecord, key, deep: bool
) -> dict:
    problems: list[str] = []
    stored_digest = hashlib.sha256()
    stored_bytes = 0
    plaintext_digest = hashlib.sha256()
    plaintext_bytes = 0
    try:
        with destination.open(backup_id, component.key) as handle:
            if key is not None and deep:
                # Digest the ciphertext on the way past rather than reading the object
                # twice: a second read of a 200 GB component doubles the verification
                # window and, on S3, its cost.
                tee = _TeeReader(handle, stored_digest)
                sink = _HashingSink(plaintext_digest)
                plaintext_bytes = decrypt_stream(
                    tee, sink, key, expect_context=_context(backup_id, component.name)
                )
                stored_bytes = tee.bytes_read
            else:
                while chunk := handle.read(_VERIFY_CHUNK):
                    stored_digest.update(chunk)
                    stored_bytes += len(chunk)
    except (DestinationError, BackupCryptoError) as exc:
        problems.append(f"{component.name}: {exc}")
        return {"name": component.name, "ok": False, "problems": problems}

    if stored_bytes != component.stored_bytes:
        problems.append(
            f"{component.name}: stored size is {stored_bytes}, manifest says "
            f"{component.stored_bytes}"
        )
    if stored_digest.hexdigest() != component.stored_sha256:
        problems.append(f"{component.name}: stored checksum does not match the manifest")
    if key is not None and deep:
        if plaintext_bytes != component.plaintext_bytes:
            problems.append(
                f"{component.name}: decrypts to {plaintext_bytes} bytes, manifest says "
                f"{component.plaintext_bytes}"
            )
        if plaintext_digest.hexdigest() != component.plaintext_sha256:
            problems.append(f"{component.name}: decrypted checksum does not match the manifest")
    return {
        "name": component.name,
        "ok": not problems,
        "stored_bytes": stored_bytes,
        "decrypted": key is not None and deep,
        "problems": problems,
    }


# --------------------------------------------------------------------------- retention


def prune_backups(
    config: AppConfig,
    *,
    destination: Destination | None = None,
    dry_run: bool = False,
    keep_id: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> dict:
    """Apply the retention rules. ``keep_id`` is never pruned, whatever they say.

    The backup that was just taken is protected explicitly rather than relying on it
    sorting newest: a clock that has gone backwards would otherwise let a run delete the
    very backup it had just written.
    """
    target = destination or build_destination(
        config.backup.destination, destination_secrets(session_factory)
    )
    decisions = plan_retention(target.list_backups(), config.backup.retention)
    outcome = summarize(decisions)
    removed: list[str] = []
    if not dry_run:
        for decision in decisions:
            if decision.keep or decision.backup_id == keep_id:
                continue
            try:
                target.delete_backup(decision.backup_id)
            except DestinationError as exc:
                _log(f"could not prune {decision.backup_id}: {exc}")
                continue
            removed.append(decision.backup_id)
            _log(f"pruned {decision.backup_id} (retention)")
        if removed and hasattr(target, "prune"):
            # A deduplicating destination separates forgetting from reclaiming, because
            # reclaiming rewrites pack files and doing it once per retention pass costs a
            # fraction of doing it once per backup that pass removes.
            emit_prune = getattr(target, "prune")
            try:
                emit_prune()
            except Exception as exc:  # noqa: BLE001 - space not reclaimed is not data lost
                _log(f"could not reclaim space after pruning: {type(exc).__name__}: {exc}")
    outcome["pruned"] = removed if not dry_run else outcome["pruned"]
    outcome["dry_run"] = dry_run
    return outcome


# --------------------------------------------------------------------------- preflight


def preflight(config: AppConfig, session_factory: sessionmaker[Session] | None = None) -> dict:
    """Everything that has to be true for tonight's backup to work, checked now.

    The failure this feature is most exposed to is silent: a schedule that has been
    running against an unmounted share since March. This is what an operator opens to find
    that out on a Tuesday afternoon instead of during a recovery.
    """
    report: dict[str, Any] = {"enabled": config.backup.enabled, "problems": []}
    # Nothing below is a problem while backups are switched off, which is how every
    # appliance starts. A fresh install that opens this page to a wall of red — no key,
    # no destination, stores "not ready" — has been told it is broken when it is simply
    # not set up yet, and the one report that matters later is the one it has learned to
    # ignore. Everything is still *reported*; it is just not called a fault.
    advisory = not config.backup.enabled

    def note(message: str, *, warning: bool = False) -> None:
        bucket = "warnings" if (warning or advisory) else "problems"
        report.setdefault(bucket, [])
        report[bucket].append(message)

    try:
        destination = build_destination(
            config.backup.destination, destination_secrets(session_factory)
        )
        report["destination"] = destination.describe()
        try:
            destination.check_writable()
            report["destination"]["writable"] = True
        except DestinationError as exc:
            report["destination"]["writable"] = False
            note(str(exc))
    except DestinationError as exc:
        report["destination"] = {"kind": config.backup.destination.kind, "error": str(exc)}
        note(str(exc))

    key_status = backup_secrets.status(backup_secrets.ENCRYPTION_KEY, session_factory)
    if config.backup.encryption_is_guaranteed:
        report["encryption"] = {
            "enabled": True,
            "key_set": key_status.set,
            "key_fingerprint": key_status.fingerprint,
            "performed_by": "destination" if not config.backup.encrypt else "appliance",
        }
        if config.backup.encrypt and not key_status.set:
            note(
                "No backup key is set yet. Open Backup → Security and press Generate — the "
                "appliance makes one, shows it to you once, and stores it. Keep the copy it "
                "shows you somewhere off this machine: a key that only exists here cannot "
                "open the backups after the day this machine is gone."
            )
    else:
        report["encryption"] = {"enabled": False, "key_set": key_status.set}
        note(
            "Backups are not encrypted. They hold privileged client documents and leave "
            "this appliance for storage it does not control."
        )

    plans = component_module.plan(config)
    report["components"] = [item.payload() for item in plans]
    for item in plans:
        if item.enabled and not item.ready and item.problem:
            # Only the appliance's own database is fatal. Everything else degrades to a
            # warning, because a firm with no Langfuse, or one that has not uploaded a
            # file yet, should still get a backup of everything it does have.
            note(f"{item.name}: {item.problem}", warning=item.name != "postgres/ki")

    staging = component_module.staging_root()
    report["staging"] = {"path": str(staging), "exists": staging.is_dir()}
    if session_factory is not None:
        report["unsettled_documents"] = count_unsettled(session_factory)
        report["last_backup"] = last_backup_run(session_factory)
        report["schedule"] = _schedule_health(config, session_factory, report)
    report.setdefault("warnings", [])
    report["ok"] = not report["problems"]
    return report


def _schedule_health(
    config: AppConfig, session_factory: sessionmaker[Session], report: dict
) -> dict:
    """Whether anything is actually watching the clock, and when it will next fire.

    A schedule is two things that can disagree: a setting in the admin UI, and a loop in
    some process. Only the first is visible from the configuration, so a firm can switch
    nightly backups on, see them switched on for eight months, and have had none — which
    is the same silent nothing this feature exists to prevent, wearing the feature's own
    clothes. This is the check that makes the disagreement visible.
    """
    from knowledge_index.backup import scheduler as backup_scheduler

    schedule = config.backup.schedule
    heartbeat = backup_scheduler.read_heartbeat(session_factory)
    health: dict[str, Any] = {
        "enabled": schedule.enabled,
        "timezone": schedule.timezone,
        "at": f"{schedule.hour:02d}:{schedule.minute:02d}",
        "heartbeat": heartbeat,
        "watcher_alive": bool(heartbeat and heartbeat["alive"]),
    }
    try:
        now = datetime.now(UTC)
        health["last_occurrence"] = backup_scheduler.latest_occurrence(config, now).isoformat()
    except Exception:  # noqa: BLE001 - a broken timezone is reported by validation, not here
        health["last_occurrence"] = None
    if schedule.enabled and not health["watcher_alive"]:
        report.setdefault("problems", []).append(
            "the backup schedule is switched on, but no scheduler has looked at the clock "
            + (
                f"in {heartbeat['age_seconds'] / 60:.0f} minutes"
                if heartbeat
                else "at all since this appliance started"
            )
            + ". Nothing will run on its own. The loop runs inside `ki serve`; check that "
            "the app container is running it and that KI_BACKUP_SCHEDULE_SECONDS is not 0."
        )
    return health


def count_unsettled(session_factory: sessionmaker[Session]) -> int:
    """Documents still mid-pipeline, which is what makes a backup internally inconsistent."""
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ProcessingState)
                .where(ProcessingState.status.in_(_UNSETTLED_STATES))
            )
            or 0
        )


def last_backup_run(session_factory: sessionmaker[Session]) -> dict | None:
    with session_factory() as session:
        record = session.scalars(
            select(PipelineRunRecord)
            .where(PipelineRunRecord.workflow == WORKFLOW)
            .order_by(PipelineRunRecord.created_at.desc())
            .limit(1)
        ).first()
        if record is None:
            return None
        return {
            "run_id": record.id,
            "status": record.status,
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            "counters": dict(record.counters or {}),
            "error": record.last_error,
        }


# -------------------------------------------------------------------------- run record


def _progress_writer(
    session_factory: sessionmaker[Session], run_id: str
) -> Callable[[str, dict], None]:
    """Publish progress on a connection of its own, as sync does.

    The fraction here is honest: the denominator is the number of components the plan
    says will be captured, which is known before the first one starts.
    """

    def publish(step: str, counters: dict) -> None:
        with session_factory() as session:
            record = session.get(PipelineRunRecord, run_id)
            if record is None:
                return
            merged = dict(record.counters or {})
            merged.update(counters)
            record.counters = merged
            if step:
                # An empty step means "write these counters down, the stage has not
                # changed". Taking and giving back the staging directory is bookkeeping
                # rather than progress, and inventing a stage for it would overwrite the
                # one line that says where a backup actually stopped.
                record.current_step = step[:60]
            planned = merged.get("components_planned")
            captured = merged.get("components_captured") or 0
            if planned:
                record.progress = min(0.99, captured / float(planned))
            session.commit()

    return publish


def _complete_run(
    session_factory: sessionmaker[Session], run_id: str, summary: dict
) -> None:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            return
        counters = dict(record.counters or {})
        counters.update(
            {
                "backup_id": summary["backup_id"],
                "components_captured": summary["components_captured"],
                "components_planned": summary["components_planned"],
                "bytes_stored": summary["bytes_stored"],
                "bytes_plaintext": summary["bytes_plaintext"],
                "warnings": summary["warnings"],
                "verified": bool((summary.get("verified") or {}).get("ok")),
                "pruned": summary.get("pruned") or [],
                "seconds": summary["seconds"],
            }
        )
        record.counters = counters
        record.status = "completed"
        record.progress = 1
        record.current_step = "complete" if not summary["warnings"] else "complete (with warnings)"
        record.finished_at = record.finished_at or datetime.now(UTC)
        session.commit()


def _fail_run(
    session_factory: sessionmaker[Session], run_id: str, exc: BaseException, *, step: str
) -> None:
    with session_factory() as session:
        record = session.get(PipelineRunRecord, run_id)
        if record is None:
            return
        record.status = "failed"
        record.current_step = step[:60]
        record.last_error = {"class": type(exc).__name__, "message": str(exc)}
        record.finished_at = datetime.now(UTC)
        session.commit()


# ----------------------------------------------------------------------------- helpers


class _TeeReader:
    """Reads through, digesting as it goes, so one pass answers two questions."""

    def __init__(self, wrapped, digest) -> None:
        self._wrapped = wrapped
        self._digest = digest
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._wrapped.read(size)
        self._digest.update(chunk)
        self.bytes_read += len(chunk)
        return chunk


class _HashingSink:
    """A write target that keeps the digest and throws the bytes away.

    Verification has to decrypt a component in full to prove it decrypts, but has nowhere
    to put a 200 GB dump and no reason to keep it.
    """

    def __init__(self, digest) -> None:
        self._digest = digest

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        return len(data)


def _appliance_metadata(config: AppConfig, session_factory: sessionmaker[Session] | None) -> dict:
    """The facts a restore has to check before it touches anything."""
    from knowledge_index.connectors.runtime.secrets import key_fingerprint as connector_fingerprint

    metadata: dict[str, Any] = {
        "index_name": config.retrieval.index_name,
        "embedding_signature": config.embedding_signature(),
        "embedding_dimensions": config.retrieval.embedding_dimensions,
    }
    try:
        from importlib.metadata import version

        metadata["package_version"] = version("knowledge-index")
    except Exception:  # noqa: BLE001 - a source checkout has no installed distribution
        metadata["package_version"] = "unknown"
    try:
        metadata["connector_key_fingerprint"] = connector_fingerprint()
    except Exception:  # noqa: BLE001 - reported as a warning by preflight, not fatal here
        metadata["connector_key_fingerprint"] = None
    if session_factory is not None:
        with session_factory() as session:
            try:
                metadata["alembic_revision"] = session.scalar(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                )
            except Exception:  # noqa: BLE001 - a create_all schema has no alembic table
                metadata["alembic_revision"] = None
    return metadata


def _config_digest(config: AppConfig) -> str:
    """Digest of the effective configuration, so a restore can see it changed."""
    return hashlib.sha256(
        config.model_dump_json(exclude={"backup"}).encode("utf-8")
    ).hexdigest()


def _advisory_xact_lock(session: Session, key: str) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    lock_id = int.from_bytes(
        hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(), byteorder="big", signed=True
    )
    session.execute(select(func.pg_advisory_xact_lock(lock_id)))
