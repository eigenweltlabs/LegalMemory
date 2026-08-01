"""Getting a backup back, and refusing to do it wrong.

Restoring is the operation that has to work on the worst day this firm has, performed by
somebody who has not done it before, possibly onto hardware that is not the hardware the
backup came from. So this module is built around two ideas.

The first is that staging and applying are separate. :func:`stage_backup` downloads,
decrypts and checksums a backup into a directory and touches nothing else — it is safe to
run on a live appliance, at any time, and it is what turns "we have backups" into "we have
*restorable* backups". Only the ``apply_*`` functions change anything, and each one takes
a single store.

The second is that a restore checks compatibility before it starts, not after. Four facts
in the manifest decide whether this backup can become this appliance: the manifest schema,
the schema revision the dump was taken at, the embedding signature the index was built
with, and the fingerprint of the connector credential key. The last is the one that ruins
recoveries quietly — restore a database under a different ``KI_CONNECTOR_CREDENTIAL_KEY``
and everything works until the first token refresh, at which point every connector the
firm has authorized is dead and nobody knows why. :func:`restore_plan` reports it as a
blocker, in advance, in words.

Two stores are deliberately not restorable from here: Keycloak's data volume and Hatchet's
config volume. Both belong to containers that have to be stopped before their volume can
be replaced, and a process running inside the stack cannot stop the stack it is running
in. ``scripts/restore-backup.sh`` does those, and :func:`restore_plan` says so rather than
pretending they are covered.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from knowledge_index.backup.components import libpq_target, opensearch_repository
from knowledge_index.backup.crypto import decrypt_stream, key_fingerprint, load_key
from knowledge_index.backup import runs as backup_runs
from knowledge_index.backup.runs import load_manifest
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig

_COPY_BYTES = 1024 * 1024
_PG_RESTORE_TIMEOUT_SECONDS = float(
    os.environ.get("KI_BACKUP_PG_RESTORE_TIMEOUT_SECONDS", "21600")
)
# Stores whose restore needs the owning container stopped. Not "impossible from here" any
# more: the restore agent stops exactly those containers and replaces exactly those
# volumes, so this is the list of what needs it, and whether it can be done is a question
# about whether the agent is running.
NEEDS_CONTAINER_STOPPED = ("volumes/keycloak", "volumes/hatchet-config")
OFFLINE_ONLY = NEEDS_CONTAINER_STOPPED

# pg_restore exits non-zero for errors that cannot have cost a single row, and it does so
# on every restore where the dump was written by a newer pg_dump than the server it is
# being loaded into: the dump opens by SETting a GUC the older server has never heard of.
# That is the normal case here — the image ships pg_dump 17 and talks to Postgres 16 — so
# treating a non-zero exit as a failed restore would report failure every time, which is
# the fastest way to teach an operator to ignore the report that matters. Kept deliberately
# short: anything not on this list is serious, and a firm that meets a genuinely harmless
# error not listed here should see it and decide, rather than have it hidden in advance.
_BENIGN_RESTORE_ERRORS = ("unrecognized configuration parameter",)


def _log(message: str) -> None:
    print(f"[ki restore] {message}", file=sys.stderr, flush=True)


class RestoreError(RuntimeError):
    """A restore could not be performed, or must not be."""


@dataclass
class StagedFile:
    name: str
    kind: str
    path: Path
    bytes: int
    detail: dict


# ------------------------------------------------------------------------------- plan


def restore_plan(
    config: AppConfig,
    backup_id: str,
    session_factory: sessionmaker[Session] | None = None,
    *,
    source_path: str | None = None,
) -> dict:
    """What restoring this backup would do, and what stands in the way.

    Blockers stop a restore. Warnings are things an operator must read and accept — a
    schema revision that is not the one this build expects, an embedding signature that no
    longer matches the configured model. Neither is decided here; both are reported.
    """
    from knowledge_index.backup import restore_runs

    destination = restore_runs.destination_for(config, source_path, session_factory)
    manifest = load_manifest(
        config, backup_id, destination=destination, session_factory=session_factory
    )
    blockers: list[str] = []
    warnings: list[str] = []

    if any(item.encrypted for item in manifest.components):
        try:
            key = load_key(backup_runs.destination_secrets(session_factory).get("encryption_key"))
        except Exception as exc:  # noqa: BLE001 - reported, not raised: this is a report
            blockers.append(str(exc))
        else:
            recorded = (manifest.encryption or {}).get("key_fingerprint")
            if recorded and recorded != key_fingerprint(key):
                blockers.append(
                    f"this backup was encrypted under key {recorded} but the key set on "
                    f"this appliance is {key_fingerprint(key)}. There is no way to open it "
                    "without the original key."
                )

    recorded_connector_key = manifest.appliance.get("connector_key_fingerprint")
    current_connector_key = _current_connector_fingerprint()
    if recorded_connector_key and current_connector_key:
        if recorded_connector_key != current_connector_key:
            blockers.append(
                f"the connector credential key does not match: this backup's database was "
                f"encrypted under KI_CONNECTOR_CREDENTIAL_KEY {recorded_connector_key}, this "
                f"deployment has {current_connector_key}. Restoring anyway leaves every "
                "stored OAuth token undecryptable and every connector needing to be "
                "re-authorized by hand. Set the key from this backup's secrets/environment "
                "component before restoring."
            )
    elif recorded_connector_key and not current_connector_key:
        warnings.append(
            "KI_CONNECTOR_CREDENTIAL_KEY is not set here, so it cannot be checked against "
            f"the {recorded_connector_key} this backup was taken under"
        )

    recorded_revision = manifest.appliance.get("alembic_revision")
    if recorded_revision and recorded_revision != _current_alembic_head():
        warnings.append(
            f"the dump was taken at schema revision {recorded_revision}; this build's head "
            f"is {_current_alembic_head()}. Restore it and let the app migrate forward on "
            "start — but never restore a dump from a *newer* build into an older one."
        )

    recorded_signature = manifest.appliance.get("embedding_signature")
    if recorded_signature and recorded_signature != config.embedding_signature():
        warnings.append(
            f"the index in this backup was built with embedding signature "
            f"{recorded_signature}; this appliance is configured for "
            f"{config.embedding_signature()}. Restore the index and the vectors will not "
            "match the configured model — plan on a rebuild."
        )

    steps = []
    for component in manifest.components:
        steps.append(
            {
                "name": component.name,
                "kind": component.kind,
                "bytes": component.plaintext_bytes,
                "restorable_here": component.name not in NEEDS_CONTAINER_STOPPED
                or _agent_available(),
                "how": _how(component.name, component.kind),
            }
        )
    return {
        "backup_id": backup_id,
        "created_at": manifest.created_at,
        "appliance": dict(manifest.appliance),
        "blockers": blockers,
        "warnings": warnings + list(manifest.warnings),
        "steps": steps,
        "ok": not blockers,
    }


def _agent_available() -> bool:
    from knowledge_index.backup import volume_agent

    return volume_agent.available()


def _how(name: str, kind: str) -> str:
    if name in NEEDS_CONTAINER_STOPPED:
        from knowledge_index.backup import volume_agent

        return (
            "stop the owning container and replace the volume — done here by the restore agent"
            if volume_agent.available()
            else "stop the stack and replace the volume — scripts/restore-backup.sh"
        )
    if kind == "postgres":
        return "pg_restore --clean --if-exists"
    if kind == "opensearch":
        return "register the repository, then the snapshot restore API"
    if kind == "secrets":
        return "read and place into the deployment's secret material by hand"
    return "extract over the directory, with the app and worker stopped"


# ------------------------------------------------------------------------------ stage


def stage_backup(
    config: AppConfig,
    backup_id: str,
    target: Path,
    *,
    only: list[str] | None = None,
    reuse: bool = False,
    session_factory: sessionmaker[Session] | None = None,
    source_path: str | None = None,
) -> list[StagedFile]:
    """Download, decrypt and verify a backup into ``target``. Changes nothing else.

    Every component's checksum is re-checked against the manifest as it lands. A staged
    directory is therefore a backup that has been *proven* readable, which is the only
    kind worth practising a restore from.

    ``reuse`` keeps an already-staged component if it still hashes to what the manifest
    says, instead of fetching it again. The guarantee is unchanged — nothing is applied
    that has not been checked against the manifest in this call — but a whole-appliance
    restore stages once rather than transferring and decrypting the estate a second time
    on the day that time matters most.
    """
    from knowledge_index.backup import restore_runs

    destination = restore_runs.destination_for(config, source_path, session_factory)
    manifest = load_manifest(
        config, backup_id, destination=destination, session_factory=session_factory
    )
    key = None
    if any(item.encrypted for item in manifest.components):
        key = load_key(backup_runs.destination_secrets(session_factory).get("encryption_key"))
    # Staging is where an encrypted backup becomes plaintext again: the database dumps of
    # the whole estate, and environment.json with KI_CONNECTOR_CREDENTIAL_KEY in it. The
    # documentation asks operators to do this on a live appliance as a drill, so leaving it
    # at the default 0644 would make every drill a copy of the firm's documents that any
    # local account can read. Owner-only, and the directory too, in case it is new.
    target.mkdir(parents=True, exist_ok=True)
    _make_private(target)
    _write_private(target / "manifest.json", manifest.to_json().encode("utf-8"))

    staged: list[StagedFile] = []
    for component in manifest.components:
        if only and component.name not in only:
            continue
        output = target / Path(component.key).name
        if reuse and _already_staged(output, component):
            _log(f"reusing {component.name} <- {output}")
            staged.append(
                StagedFile(
                    name=component.name,
                    kind=component.kind,
                    path=output,
                    bytes=component.plaintext_bytes,
                    detail=dict(component.detail),
                )
            )
            continue
        digest = hashlib.sha256()
        written = 0
        _log(f"staging {component.name} -> {output}")
        with destination.open(backup_id, component.key) as source, _open_private(output) as sink:
            if component.encrypted:
                written = decrypt_stream(
                    source,
                    _DigestingWriter(sink, digest),
                    key,
                    # A component's own tags say which backup and which component it was
                    # sealed as, so a substituted one is refused here even though the
                    # manifest that named it was rewritten to match.
                    expect_context={"backup_id": backup_id, "component": component.name},
                )
            else:
                while chunk := source.read(_COPY_BYTES):
                    digest.update(chunk)
                    sink.write(chunk)
                    written += len(chunk)
        if written != component.plaintext_bytes or digest.hexdigest() != component.plaintext_sha256:
            output.unlink(missing_ok=True)
            raise RestoreError(
                f"{component.name} did not match its manifest entry after staging "
                f"({written} bytes, digest {digest.hexdigest()[:12]}…). This backup is "
                "damaged; do not restore from it."
            )
        staged.append(
            StagedFile(
                name=component.name,
                kind=component.kind,
                path=output,
                bytes=written,
                detail=dict(component.detail),
            )
        )
    return staged


# ------------------------------------------------------------------------------ apply


def apply_database(config: AppConfig, staged: StagedFile, *, target_url: str | None = None) -> dict:
    """``pg_restore`` one staged dump over a live database.

    ``--clean --if-exists`` drops what it is about to recreate, which is what makes a
    restore a restore rather than a merge. It is also destructive to whatever is there
    now, which is why nothing calls this without an explicit instruction from an operator.

    It is destructive to whatever is *connected*, too, and that part is not visible here. A
    service holding a pool across this call keeps cached query plans and type OIDs for
    objects that have just been dropped and recreated, and Postgres answers it with "cached
    plan must not change result type" and "cache lookup failed for type ..." from then on.
    Restoring a database therefore leaves the service that owns it needing a restart, which
    is :func:`knowledge_index.backup.restore_runs.services_to_restart`'s job — do not call
    this on its own and assume the appliance is well afterwards.
    """
    # The component's own name — postgres/ki, postgres/litellm — is what identifies which
    # configured database this dump belongs to. The name recorded in the manifest detail is
    # what the database was *called* on the appliance it came from, which is a different
    # thing: a firm whose database is not literally named "ki" would find every restore
    # refused with "no connection URL is configured", having done nothing wrong.
    target_name = staged.name.split("/", 1)[-1]
    database = staged.detail.get("database") or target_name
    url = target_url or _url_for(config, target_name)
    uri, password = libpq_target(url)
    environment = dict(os.environ)
    if password:
        environment["PGPASSWORD"] = password
    command = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        # Deliberately not --exit-on-error. A dump from a live appliance holds objects
        # whose drop order pg_restore cannot fully resolve — pgvector's types and operator
        # classes are the usual ones — and stopping at the first of those would abandon a
        # restore that is otherwise complete. Every error line is collected and returned
        # instead, so the operator sees them and decides.
        "--verbose",
        f"--dbname={uri}",
        str(staged.path),
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            text=True,
            env=environment,
            timeout=_PG_RESTORE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RestoreError("pg_restore is not installed in this image") from exc
    errors = [line for line in completed.stderr.splitlines() if "error:" in line.lower()]
    if completed.returncode != 0 and not errors:
        raise RestoreError(f"pg_restore of {database} failed ({completed.returncode})")
    serious = [line for line in errors if not _is_benign(line)]
    return {
        "component": staged.name,
        "database": database,
        # The caller's verdict, decided here where the exit code and the error text are
        # both in hand. Without it a restore that dropped half the schema is a dict with a
        # non-zero returncode buried in it, printed as JSON beside the successes.
        "ok": not serious,
        "returncode": completed.returncode,
        "errors": errors[:50],
        "serious_errors": serious[:50],
        "seconds": round(time.monotonic() - started, 1),
    }


def _is_benign(line: str) -> bool:
    lowered = line.lower()
    return any(pattern in lowered for pattern in _BENIGN_RESTORE_ERRORS)


def apply_search_index(config: AppConfig, staged: StagedFile) -> dict:
    """Unpack the snapshot repository and restore every index it holds.

    Existing indices with the same names are closed first: OpenSearch refuses to restore
    over an open index, and the alternative — restoring under a renamed index — leaves the
    appliance pointing at the old data with the new data invisible beside it.
    """
    repository, container_path, local_path = opensearch_repository()
    if not container_path or not str(local_path):
        raise RestoreError(
            "the OpenSearch snapshot repository is not configured in this container; the "
            "search index cannot be restored from here"
        )
    if not local_path.is_dir():
        raise RestoreError(f"the snapshot repository volume is not mounted at {local_path}")
    for entry in local_path.iterdir():
        # The repository has to contain this backup's snapshot and nothing else, or
        # OpenSearch will read stale index-N metadata alongside it.
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    _safe_extract(staged.path, local_path)

    base = config.components.opensearch_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=120.0) as client:
        response = client.put(
            f"/_snapshot/{repository}",
            json={"type": "fs", "settings": {"location": container_path, "compress": True}},
        )
        if response.status_code >= 400:
            raise RestoreError(f"could not register the snapshot repository: {response.text[:400]}")
        listing = client.get(f"/_snapshot/{repository}/_all")
        snapshots = [item["snapshot"] for item in (listing.json().get("snapshots") or [])]
        if not snapshots:
            raise RestoreError("the staged repository contains no snapshots")
        snapshot = snapshots[-1]
        detail = client.get(f"/_snapshot/{repository}/{snapshot}").json()["snapshots"][0]
        indices = list(detail.get("indices") or [])
        for index in indices:
            client.post(f"/{index}/_close")
        restore = client.post(
            f"/_snapshot/{repository}/{snapshot}/_restore?wait_for_completion=true",
            json={"indices": ",".join(indices), "include_global_state": True, "partial": False},
        )
        if restore.status_code >= 400:
            raise RestoreError(f"snapshot restore failed: {restore.text[:400]}")
        for index in indices:
            client.post(f"/{index}/_open")
    return {"component": staged.name, "snapshot": snapshot, "indices": indices}


def apply_volume(config: AppConfig, staged: StagedFile) -> dict:
    """Replace a volume owned by another container, through the restore agent.

    Not done in this process for the reason the agent exists: the container holding the
    volume has to stop first, and a process inside the stack cannot stop the stack. The
    agent holds the Docker socket so that nothing else here has to.
    """
    del config
    from knowledge_index.backup import volume_agent

    try:
        outcome = volume_agent.replace_volume(staged.name, staged.path)
    except RuntimeError as exc:
        raise RestoreError(str(exc)) from exc
    return {"component": staged.name, "ok": True, "service": outcome.get("service"), "errors": []}


def apply_files(config: AppConfig, staged: StagedFile) -> dict:
    """Extract a staged archive back over the directory it came from.

    Only meaningful with the app, worker and watcher stopped: extracting the blob store
    underneath a running pipeline gives it files it has already decided are missing.
    """
    target = _fileset_target(config, staged.name)
    if target is None:
        raise RestoreError(f"no restore target is known for {staged.name}")
    target.mkdir(parents=True, exist_ok=True)
    _safe_extract(staged.path, target)
    return {"component": staged.name, "target": str(target)}


def _fileset_target(config: AppConfig, name: str) -> Path | None:
    artifacts = config.artifact_dir.expanduser()
    return {
        "files/artifact-blobs": artifacts / "blobs",
        "files/uploaded": artifacts.parent / "browser-sources",
        "files/connector-staging": artifacts / "connector-staging",
        "files/watched": artifacts.parent / "watched",
        # Extracted over the data root, which puts config.json back where the appliance
        # reads it. The archive holds top-level files only, so nothing else there is
        # touched.
        "files/appliance-config": artifacts.parent,
    }.get(name)


def _safe_extract(archive: Path, target: Path) -> None:
    """Extract a tar without letting it write outside ``target``.

    A backup archive is written by this appliance, but it is read back from a share that
    other people can write to, so it is treated as untrusted input on the way in. Python's
    ``data`` filter rejects absolute paths, parent traversal, links pointing outside the
    destination, and device nodes; the explicit check afterwards is there because that
    filter is a recent addition and this must not depend on the interpreter's patch level.
    """
    # "r:*" rather than "r:gz": a backup written to a deduplicating destination stores its
    # archives uncompressed so the chunker can see what did not change, and a restore has
    # to read both shapes without being told which it is looking at.
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            resolved = (target / member.name).resolve()
            if target.resolve() != resolved and target.resolve() not in resolved.parents:
                raise RestoreError(f"archive entry escapes the target directory: {member.name!r}")
        try:
            tar.extractall(target, filter="data")
        except TypeError:  # Python without PEP 706 filters; the check above already ran
            tar.extractall(target)  # noqa: S202


def _url_for(config: AppConfig, database: str) -> str:
    from knowledge_index.backup.components import database_targets

    for target in database_targets(config):
        if target.name == database and target.url:
            return target.url
    raise RestoreError(f"no connection URL is configured for database {database!r}")


def _current_connector_fingerprint() -> str | None:
    try:
        from knowledge_index.connectors.runtime.secrets import key_fingerprint as fingerprint

        return fingerprint()
    except Exception:  # noqa: BLE001 - an unset key is a reportable state, not an error here
        return None


def _current_alembic_head() -> str | None:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:
        return None
    for base in [Path.cwd(), *Path(__file__).resolve().parents]:
        ini = base / "alembic.ini"
        if ini.exists() and (base / "migrations").is_dir():
            try:
                config = Config(str(ini))
                config.set_main_option("script_location", str(base / "migrations"))
                return ScriptDirectory.from_config(config).get_current_head()
            except Exception:  # noqa: BLE001 - best effort; reported as "unknown"
                return None
    return None


def _already_staged(path: Path, component) -> bool:
    """Whether ``path`` is this component, proven by hashing it rather than by trusting it.

    Size first because it is free, then the full digest: a local re-hash is a fraction of
    the cost of pulling the component off a NAS and decrypting it again, and it keeps the
    rule that nothing is applied without having been checked in this call.
    """
    try:
        if not path.is_file() or path.stat().st_size != component.plaintext_bytes:
            return False
    except OSError:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_BYTES):
            digest.update(chunk)
    return digest.hexdigest() == component.plaintext_sha256


def _open_private(path: Path):
    """Create a file only its owner can read, before a single byte goes into it.

    Opened with the mode rather than chmod'ed afterwards, because the window between
    creating a world-readable file and narrowing it is the whole of the exposure.
    """
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")


def _write_private(path: Path, payload: bytes) -> None:
    with _open_private(path) as handle:
        handle.write(payload)


def _make_private(directory: Path) -> None:
    """Owner-only on the staging directory. Best effort: a bind mount may refuse."""
    try:
        directory.chmod(0o700)
    except OSError:  # noqa: BLE001 - a mounted share can own its own permissions
        _log(f"could not restrict permissions on {directory}; check who can read it")


class _DigestingWriter:
    def __init__(self, wrapped, digest) -> None:
        self._wrapped = wrapped
        self._digest = digest

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        return self._wrapped.write(data)


__all__ = [
    "RestoreError",
    "StagedFile",
    "apply_database",
    "apply_files",
    "apply_search_index",
    "apply_volume",
    "restore_plan",
    "stage_backup",
]
