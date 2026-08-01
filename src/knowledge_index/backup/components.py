"""Capturing each store, and knowing beforehand whether it can be captured.

Every store this appliance keeps is reached the way it is meant to be reached: Postgres
through ``pg_dump`` over the wire, OpenSearch through its snapshot API, and the two stores
that have no protocol — Keycloak's embedded database and Hatchet's generated config —
through read-only mounts of their volumes. Nothing here stops a container or reaches into
another container's filesystem behind its back, which is what makes a backup something the
appliance can do to itself, on a schedule, while it is running.

The two halves of this module exist for the same reason. :func:`plan` answers "could a
backup run right now, and what would be in it" without writing anything, because the
failure this feature is most likely to have is not a crash — it is running nightly for
eight months against a share that was unmounted in March. :func:`collect` then performs
exactly the plan it reported.

Where a store is reached is deployment layout, not policy, so those paths are environment
variables read here rather than settings in ``config.json``: the compose file already
knows where it mounted things, and an administrator should not have to keep a second copy
of that knowledge correct in the admin UI.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy.engine import URL, make_url

from knowledge_index.config import AppConfig

# Where components are staged before they are sent to the destination. One component at a
# time, deleted as soon as it has been transferred, so the requirement is room for the
# largest single store — not for the whole backup.
STAGING_DIR_ENV = "KI_BACKUP_STAGING_DIR"
DEFAULT_STAGING_DIR = "/data/backup-staging"

# Read-only mounts of volumes owned by other containers. Empty means "not mounted here",
# which is reported as a skipped component rather than a failure: a single-container
# development stack legitimately has neither.
KEYCLOAK_PATH_ENV = "KI_BACKUP_KEYCLOAK_PATH"
HATCHET_CONFIG_PATH_ENV = "KI_BACKUP_HATCHET_CONFIG_PATH"
# Two views of one directory: the path OpenSearch registers as its repository (inside its
# own container, which is the only path it will accept) and the path the same volume is
# mounted at here, which is where the finished snapshot is read from.
OPENSEARCH_REPO_CONTAINER_PATH_ENV = "KI_BACKUP_OPENSEARCH_REPO_CONTAINER_PATH"
OPENSEARCH_REPO_PATH_ENV = "KI_BACKUP_OPENSEARCH_REPO_PATH"
OPENSEARCH_REPO_NAME_ENV = "KI_BACKUP_OPENSEARCH_REPO_NAME"
DEFAULT_OPENSEARCH_REPO_NAME = "ki-backup"

HATCHET_DATABASE_URL_ENV = "KI_BACKUP_HATCHET_DATABASE_URL"
LITELLM_DATABASE_URL_ENV = "KI_BACKUP_LITELLM_DATABASE_URL"
LANGFUSE_DATABASE_URL_ENV = "KI_BACKUP_LANGFUSE_DATABASE_URL"
PRIMARY_DATABASE_URL_ENV = "KI_DATABASE_URL"

# Deployment secrets worth capturing, by name prefix. Narrow on purpose: the container's
# environment also holds PATH, LANG and whatever the base image sets, and a backup is not
# a place to accumulate things nobody decided to put there.
SECRET_PREFIXES = ("KI_", "LITELLM_", "HATCHET_", "KEYCLOAK_", "POSTGRES_", "LANGFUSE_")
# Never captured. Nothing here sets these any more — the backup key lives in the database,
# not in the environment — but a deployment that exports one for its own reasons must not
# have it swept into the backup that key protects.
SECRET_NEVER = ("KI_BACKUP_ENCRYPTION_KEY", "KI_BACKUP_S3_SECRET_ACCESS_KEY")

_OPENSEARCH_POLL_SECONDS = 5.0
_OPENSEARCH_SNAPSHOT_TIMEOUT_SECONDS = float(
    os.environ.get("KI_BACKUP_OPENSEARCH_TIMEOUT_SECONDS", "10800")
)
_PG_DUMP_TIMEOUT_SECONDS = float(os.environ.get("KI_BACKUP_PG_DUMP_TIMEOUT_SECONDS", "21600"))
_HASH_BYTES = 1024 * 1024


def _log(message: str) -> None:
    print(f"[ki backup] {message}", file=sys.stderr, flush=True)


class ComponentError(RuntimeError):
    """A component that was meant to be captured could not be."""


@dataclass(frozen=True)
class StagedComponent:
    """One captured store, sitting in the staging directory, ready to be transferred."""

    name: str
    kind: str
    filename: str
    path: Path
    sha256: str
    plaintext_bytes: int
    detail: dict = field(default_factory=dict)


@dataclass
class ComponentPlan:
    """What :func:`plan` reports for one store, before anything is written."""

    name: str
    kind: str
    enabled: bool
    ready: bool
    detail: dict = field(default_factory=dict)
    problem: str | None = None

    def payload(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "enabled": self.enabled,
            "ready": self.ready,
            "detail": self.detail,
            "problem": self.problem,
        }


# ------------------------------------------------------------------------ staging area


def staging_root() -> Path:
    return Path(os.environ.get(STAGING_DIR_ENV, DEFAULT_STAGING_DIR)).expanduser()


def prepare_staging(backup_id: str) -> Path:
    """A private directory for this run, cleared if a previous run died holding it."""
    directory = staging_root() / backup_id
    if directory.exists():
        shutil.rmtree(directory)
    # Owner-only: every component passes through here as plaintext on its way to being
    # sealed, so for the length of a run this directory is the whole appliance in the clear.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def clear_staging(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------- databases


@dataclass(frozen=True)
class DatabaseTarget:
    name: str
    url: str
    required: bool


def database_targets(config: AppConfig) -> list[DatabaseTarget]:
    """Every Postgres database this appliance owns, in the order they are dumped.

    LiteLLM's and Langfuse's databases live on the primary server and are created by the
    init scripts in ``deploy/postgres/init``, so their URLs are derived from the primary
    rather than configured twice — a firm that moved one elsewhere overrides it by name.
    Hatchet's is a different server entirely and has to be given.
    """
    primary = os.environ.get(PRIMARY_DATABASE_URL_ENV, "").strip()
    if not primary:
        from knowledge_index.db.engine import DEFAULT_URL

        primary = DEFAULT_URL
    targets = [DatabaseTarget("ki", primary, required=True)]
    if config.backup.sources.gateway_databases:
        targets.append(
            DatabaseTarget(
                "litellm",
                os.environ.get(LITELLM_DATABASE_URL_ENV, "").strip()
                or _with_database(primary, "litellm"),
                required=False,
            )
        )
        targets.append(
            DatabaseTarget(
                "langfuse",
                os.environ.get(LANGFUSE_DATABASE_URL_ENV, "").strip()
                or _with_database(primary, "langfuse"),
                required=False,
            )
        )
    if config.backup.sources.orchestrator_database:
        targets.append(
            DatabaseTarget("hatchet", os.environ.get(HATCHET_DATABASE_URL_ENV, "").strip(), False)
        )
    return targets


def _with_database(url: str, database: str) -> str:
    """The same server, a different database — with the password intact.

    Rebuilt rather than edited for the same reason as :func:`libpq_target`: ``str`` on a
    SQLAlchemy URL renders the password as ``***``, so the obvious one-liner produces a URL
    that looks right, carries a literal ``***`` where the password belongs, and fails
    authentication against every database derived from it. That is what made the LiteLLM
    and Langfuse dumps fail while the appliance's own succeeded.
    """
    parsed = make_url(url)
    return URL.create(
        parsed.drivername,
        username=parsed.username,
        password=parsed.password,
        host=parsed.host,
        port=parsed.port,
        database=database,
        query=parsed.query,
    ).render_as_string(hide_password=False)


def libpq_target(url: str) -> tuple[str, str | None]:
    """Split a SQLAlchemy URL into a libpq URI and the password, kept out of the URI.

    The password goes to ``pg_dump`` in ``PGPASSWORD`` rather than in the connection
    string, because an argument is visible in ``ps`` to anything sharing the namespace and
    ends up in any process listing an operator pastes into a ticket.
    """
    parsed = make_url(url)
    password = parsed.password
    # Rebuilt rather than edited, because neither half of the obvious version works:
    # ``URL.set(password=None)`` is a no-op — ``set`` ignores None arguments — and ``str``
    # on a URL renders the password as ``***``. Together they hand pg_dump a literal
    # ``***`` to authenticate with, which libpq prefers over PGPASSWORD.
    bare = URL.create(
        # pg8000 is this appliance's driver; pg_dump only knows the bare scheme.
        "postgresql",
        username=parsed.username,
        host=parsed.host,
        port=parsed.port,
        database=parsed.database,
        query=parsed.query,
    )
    return bare.render_as_string(hide_password=False), password


def dump_database(
    target: DatabaseTarget, staging: Path, *, compress: bool = True
) -> StagedComponent:
    """``pg_dump`` one database into the staging directory.

    Custom format, because it is what ``pg_restore`` can restore selectively, in parallel,
    and into a differently-named database — the three things a real recovery needs and a
    plain SQL file cannot do. Ownership and grants are stripped so the dump restores as
    whichever superuser the recovery environment has, rather than requiring the firm to
    recreate role names before it can read its own documents back.

    ``compress`` is off when the destination deduplicates. A compressed dump turns a
    thousand changed rows into a completely different byte stream, which is precisely what
    a content-defined chunker cannot see through — so a deduplicating destination would
    store the whole database again every night and report a fine compression ratio while
    doing it. Uncompressed, last night's chunks are still last night's, and restic
    compresses what it stores after chunking rather than before.
    """
    uri, password = libpq_target(target.url)
    output = staging / f"postgres-{target.name}.dump"
    command = [
        "pg_dump",
        "--format=custom",
        f"--compress={6 if compress else 0}",
        "--no-owner",
        "--no-privileges",
        f"--file={output}",
        uri,
    ]
    environment = dict(os.environ)
    if password:
        environment["PGPASSWORD"] = password
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            capture_output=True,
            text=True,
            env=environment,
            timeout=_PG_DUMP_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ComponentError(
            "pg_dump is not installed in this image, so databases cannot be backed up. "
            "It is installed in the app and worker images this project ships; a custom "
            "image needs the postgresql-client package."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ComponentError(
            f"pg_dump of {target.name} exceeded {_PG_DUMP_TIMEOUT_SECONDS:.0f}s. Raise "
            "KI_BACKUP_PG_DUMP_TIMEOUT_SECONDS if this database is genuinely that large."
        ) from exc
    if completed.returncode != 0:
        output.unlink(missing_ok=True)
        raise ComponentError(
            f"pg_dump of {target.name} failed ({completed.returncode}): "
            f"{_last_line(completed.stderr)}"
        )
    digest, size = _digest_file(output)
    return StagedComponent(
        name=f"postgres/{target.name}",
        kind="postgres",
        filename=output.name,
        path=output,
        sha256=digest,
        plaintext_bytes=size,
        detail={
            "database": make_url(target.url).database,
            "host": make_url(target.url).host,
            "format": "pg_dump custom v1",
            "seconds": round(time.monotonic() - started, 1),
        },
    )


def probe_database(target: DatabaseTarget) -> str | None:
    """Return why a database cannot be dumped, or None if it can. Never raises."""
    if not target.url:
        return f"no URL configured ({HATCHET_DATABASE_URL_ENV} is unset)"
    uri, password = libpq_target(target.url)
    environment = dict(os.environ)
    if password:
        environment["PGPASSWORD"] = password
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["pg_isready", "--dbname", uri],
            capture_output=True,
            text=True,
            env=environment,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        return "pg_isready is not installed in this image"
    except subprocess.TimeoutExpired:
        return "the database did not answer within 15s"
    if completed.returncode != 0:
        return (
            _last_line(completed.stdout)
            or _last_line(completed.stderr)
            or "not accepting connections"
        )
    return None


# ------------------------------------------------------------------------ search index


def opensearch_repository() -> tuple[str, str, Path]:
    """The repository name, the path OpenSearch knows it by, and the path we read it at."""
    name = os.environ.get(OPENSEARCH_REPO_NAME_ENV, DEFAULT_OPENSEARCH_REPO_NAME)
    container_path = os.environ.get(OPENSEARCH_REPO_CONTAINER_PATH_ENV, "").strip()
    local_path = os.environ.get(OPENSEARCH_REPO_PATH_ENV, "").strip()
    return name, container_path, Path(local_path) if local_path else Path()


def snapshot_search_index(config: AppConfig, staging: Path, backup_id: str) -> StagedComponent:
    """Take an OpenSearch snapshot, then archive the repository it landed in.

    The snapshot API is the only supported way to get a consistent copy of a live
    OpenSearch cluster — Lucene segments are written and merged continuously, so a tar of
    the data directory of a running node is a copy of a moving target. Once the snapshot
    reports SUCCESS the repository directory is quiescent, and *that* is safe to archive.

    The snapshot is deleted from the repository afterwards so each backup carries a
    self-contained copy. It costs a full transfer every night instead of an incremental
    one, and it buys a restore that needs nothing but the backup being restored — which is
    the trade a firm wants when the reason they are restoring is that the appliance is
    gone.
    """
    repository, container_path, local_path = opensearch_repository()
    if not container_path or not local_path:
        raise ComponentError(
            f"the OpenSearch snapshot repository is not configured: set "
            f"{OPENSEARCH_REPO_CONTAINER_PATH_ENV} to the path OpenSearch has as path.repo "
            f"and {OPENSEARCH_REPO_PATH_ENV} to where that same volume is mounted here."
        )
    base = config.components.opensearch_url.rstrip("/")
    snapshot = backup_id.lower()
    started = time.monotonic()
    with httpx.Client(base_url=base, timeout=60.0) as client:
        _put_repository(client, repository, container_path)
        # A snapshot left behind by a run that died mid-flight would make this one fail
        # with "invalid snapshot name, snapshot with the same name already exists".
        client.delete(f"/_snapshot/{repository}/{snapshot}")
        response = client.put(
            f"/_snapshot/{repository}/{snapshot}",
            json={
                # Every index except OpenSearch's own dot-prefixed internals, which belong
                # to the cluster rather than to this appliance and are recreated on start.
                "indices": "*,-.*",
                "ignore_unavailable": True,
                # Cluster settings and index templates: small, and without them a restored
                # index comes back without the analyzers and mappings it was built with.
                "include_global_state": True,
                "partial": False,
            },
        )
        if response.status_code >= 400:
            raise ComponentError(f"OpenSearch refused the snapshot: {_http_detail(response)}")
        try:
            state = _await_snapshot(client, repository, snapshot)
            if state.get("state") != "SUCCESS":
                raise ComponentError(
                    f"OpenSearch snapshot finished in state {state.get('state')!r} rather "
                    f"than SUCCESS; failures: {state.get('failures')}"
                )
            indices = list(state.get("indices") or [])
            shard_stats = state.get("shards") or {}
            archive = staging / "opensearch-snapshot.tar.gz"
            digest, size, entries = _archive_directory(local_path, archive, excludes=())
        finally:
            # Whether or not any of that worked: leaving the snapshot behind grows the
            # repository volume by a full copy of the index every night.
            client.delete(f"/_snapshot/{repository}/{snapshot}")
    return StagedComponent(
        name="opensearch/snapshot",
        kind="opensearch",
        filename=archive.name,
        path=archive,
        sha256=digest,
        plaintext_bytes=size,
        detail={
            "repository": repository,
            "snapshot": snapshot,
            "indices": indices,
            "shards": shard_stats,
            "files": entries,
            "seconds": round(time.monotonic() - started, 1),
        },
    )


def _put_repository(client: httpx.Client, repository: str, location: str) -> None:
    response = client.put(
        f"/_snapshot/{repository}",
        json={"type": "fs", "settings": {"location": location, "compress": True}},
    )
    if response.status_code >= 400:
        raise ComponentError(
            f"could not register the OpenSearch snapshot repository {repository!r} at "
            f"{location!r}: {_http_detail(response)}. OpenSearch only accepts a location "
            "listed in its own path.repo setting."
        )


def _await_snapshot(client: httpx.Client, repository: str, snapshot: str) -> dict:
    deadline = time.monotonic() + _OPENSEARCH_SNAPSHOT_TIMEOUT_SECONDS
    while True:
        response = client.get(f"/_snapshot/{repository}/{snapshot}")
        if response.status_code >= 400:
            raise ComponentError(f"could not read snapshot status: {_http_detail(response)}")
        entries = response.json().get("snapshots") or []
        if not entries:
            raise ComponentError(f"OpenSearch lost snapshot {snapshot!r} while it was running")
        state = entries[0]
        if state.get("state") not in ("IN_PROGRESS", "STARTED"):
            return state
        if time.monotonic() > deadline:
            raise ComponentError(
                f"OpenSearch snapshot {snapshot!r} did not finish within "
                f"{_OPENSEARCH_SNAPSHOT_TIMEOUT_SECONDS:.0f}s"
            )
        time.sleep(_OPENSEARCH_POLL_SECONDS)


def probe_search_index(config: AppConfig) -> str | None:
    repository, container_path, local_path = opensearch_repository()
    if not container_path:
        return f"{OPENSEARCH_REPO_CONTAINER_PATH_ENV} is not set"
    if not str(local_path):
        return f"{OPENSEARCH_REPO_PATH_ENV} is not set"
    if not local_path.is_dir():
        return f"the snapshot repository volume is not mounted at {local_path}"
    try:
        with httpx.Client(base_url=config.components.opensearch_url.rstrip("/"), timeout=10) as c:
            response = c.get("/_cluster/health")
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same answer here
        return f"OpenSearch is unreachable: {type(exc).__name__}: {exc}"
    if response.status_code >= 400:
        return f"OpenSearch health check failed: {_http_detail(response)}"
    return None


# ---------------------------------------------------------------------------- filesets


def archive_directory(
    name: str,
    kind: str,
    source: Path,
    staging: Path,
    *,
    excludes: tuple[str, ...] = (),
    compress: bool = True,
) -> StagedComponent:
    suffix = "tar.gz" if compress else "tar"
    archive = staging / f"{name.replace('/', '-')}.{suffix}"
    digest, size, entries = _archive_directory(
        source, archive, excludes=excludes, compress=compress
    )
    return StagedComponent(
        name=name,
        kind=kind,
        filename=archive.name,
        path=archive,
        sha256=digest,
        plaintext_bytes=size,
        detail={"source": str(source), "files": entries, "excludes": list(excludes)},
    )


def archive_root_files(name: str, source: Path, staging: Path) -> StagedComponent:
    """Archive the files sitting directly in a directory, and none of its subdirectories.

    This exists for one file. ``config.json`` is the whole of the appliance's
    configuration — where backups go, when they run, which model does which job, how people
    sign in — and it sat beside the three directories the backup did capture, so a backup
    held a firm's documents and nothing about the appliance that served them. Restoring
    onto fresh hardware gave you back the estate and an appliance that had forgotten how it
    was set up.

    Top-level files only, because the directories beside it are captured as their own
    components and the staging and restore scratch directories must never be swept in: one
    holds the estate in plaintext mid-backup, the other mid-restore.
    """
    if not source.is_dir():
        raise ComponentError(f"nothing to archive: {source} is not a directory")
    archive = staging / f"{name.replace('/', '-')}.tar"
    entries = 0
    with tarfile.open(archive, "w") as tar:
        for item in sorted(source.iterdir()):
            if not item.is_file() or item.is_symlink():
                continue
            tar.add(item, arcname=item.name)
            entries += 1
    digest, size = _digest_file(archive)
    return StagedComponent(
        name=name,
        kind="files",
        filename=archive.name,
        path=archive,
        sha256=digest,
        plaintext_bytes=size,
        detail={"source": str(source), "files": entries, "top_level_only": True},
    )


def _archive_directory(
    source: Path, archive: Path, *, excludes: tuple[str, ...], compress: bool = True
) -> tuple[str, int, int]:
    """Write a tar of ``source``, and report its digest, size and file count.

    Entries are sorted so two backups of an unchanged directory differ only in their
    gzip timestamps, which makes "did anything actually change" answerable by eye. A file
    that disappears between the walk and the read is skipped rather than fatal: a live
    appliance is allowed to delete its own temporary files while it is being backed up.

    Uncompressed when the destination deduplicates. gzip carries its compression state
    forward, so inserting one document near the front of the archive changes every byte
    after it and a chunker sees a new file rather than one new document. Sorted entries
    plus no compression means an unchanged blob store is byte-identical to last night's
    and costs nothing to store again.
    """
    if not source.is_dir():
        raise ComponentError(f"nothing to archive: {source} is not a directory")
    entries = 0
    mode = "w:gz" if compress else "w"
    options = {"compresslevel": 6} if compress else {}
    with tarfile.open(archive, mode, **options) as tar:
        for path in sorted(source.rglob("*")):
            if any(fnmatch.fnmatch(str(path.relative_to(source)), pattern) for pattern in excludes):
                continue
            try:
                tar.add(path, arcname=str(path.relative_to(source)), recursive=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ComponentError(f"could not read {path} into the archive: {exc}") from exc
            entries += 1
    digest, size = _digest_file(archive)
    return digest, size, entries


# ----------------------------------------------------------------------------- secrets


def capture_environment_secrets(config: AppConfig, staging: Path) -> StagedComponent:
    """The deployment secrets a restore cannot reconstruct.

    Above all ``KI_CONNECTOR_CREDENTIAL_KEY``: without it the restored
    ``source_credentials`` rows are ciphertext nobody can open, and every connector the
    firm has authorized has to be authorized again by hand — the failure mode where a
    restore appears to succeed and the estate quietly stops syncing a week later.

    Only ever reached when the backup is encrypted; ``BackupConfig`` refuses to validate
    otherwise, so there is no path by which this lands on a share in the clear.
    """
    excluded = set(SECRET_NEVER)
    captured = {
        name: value
        for name, value in sorted(os.environ.items())
        if name.startswith(SECRET_PREFIXES) and name not in excluded
    }
    payload = {
        "captured_at_keys": sorted(captured),
        "excluded": sorted(excluded),
        "environment": captured,
        "note": (
            "Restore these into the deployment's own secret material (.env, systemd unit, "
            "Helm values) before starting the restored appliance. KI_CONNECTOR_CREDENTIAL_KEY "
            "must match the database dump in this same backup or connector credentials "
            "cannot be decrypted."
        ),
    }
    # Owner-only from the moment it exists: this is the one component that is written to
    # the staging disk as plaintext secrets, and it sits there until it has been encrypted
    # and transferred. Opened with the mode rather than chmod'ed afterwards, because the
    # window between creating a world-readable file and narrowing it is the exposure.
    output = staging / "environment.json"
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
        handle.write(body)
    digest, size = _digest_file(output)
    return StagedComponent(
        name="secrets/environment",
        kind="secrets",
        filename=output.name,
        path=output,
        sha256=digest,
        plaintext_bytes=size,
        # Names only. The detail block is echoed into the manifest, which is not encrypted.
        detail={"variables": sorted(captured), "excluded": sorted(excluded)},
    )


# -------------------------------------------------------------------- plan and collect


def _fileset_targets(config: AppConfig) -> list[tuple[str, str, Path, tuple[str, ...]]]:
    """(component name, kind, directory, exclude globs) for every directory component."""
    artifacts = config.artifact_dir.expanduser()
    data_root = artifacts.parent
    sources = config.backup.sources
    targets: list[tuple[str, str, Path, tuple[str, ...]]] = []
    if sources.artifact_blobs:
        # tmp holds in-flight ingest files that belong to a request nobody will resume.
        targets.append(("files/artifact-blobs", "files", artifacts / "blobs", ()))
    if sources.uploaded_files:
        targets.append(("files/uploaded", "files", data_root / "browser-sources", ()))
    if sources.connector_staging:
        targets.append(("files/connector-staging", "files", artifacts / "connector-staging", ()))
    if sources.identity_volume:
        path = os.environ.get(KEYCLOAK_PATH_ENV, "").strip()
        targets.append(("volumes/keycloak", "volume", Path(path) if path else Path(), ()))
    if sources.orchestrator_config_volume:
        path = os.environ.get(HATCHET_CONFIG_PATH_ENV, "").strip()
        targets.append(("volumes/hatchet-config", "volume", Path(path) if path else Path(), ()))
    if sources.watched_folders:
        targets.append(("files/watched", "files", data_root / "watched", ()))
    for index, extra in enumerate(sources.extra_paths):
        targets.append((f"files/extra-{index}", "files", Path(extra).expanduser(), ()))
    return targets


# The appliance's own configuration. Not a flag and not in _fileset_targets, because it is
# not a directory and because a backup without it is a backup of documents belonging to an
# appliance nobody can rebuild — the same reason postgres/ki has no toggle.
APPLIANCE_CONFIG = "files/appliance-config"


def plan(config: AppConfig) -> list[ComponentPlan]:
    """What a backup would capture right now, and what stands in the way.

    Reports rather than raises: an operator opening the backup page needs to see all four
    problems at once, not the first one. Only ``postgres/ki`` and the destination itself
    are fatal — everything else degrades to a warning recorded in the manifest, because a
    firm that has no Langfuse should still get a backup of everything it does have.
    """
    entries: list[ComponentPlan] = []
    for target in database_targets(config):
        problem = probe_database(target)
        entries.append(
            ComponentPlan(
                name=f"postgres/{target.name}",
                kind="postgres",
                enabled=True,
                ready=problem is None,
                detail={"required": target.required, "database": target.name},
                problem=problem,
            )
        )
    search_enabled = config.backup.sources.search_index
    search_problem = probe_search_index(config) if search_enabled else None
    entries.append(
        ComponentPlan(
            name="opensearch/snapshot",
            kind="opensearch",
            enabled=search_enabled,
            ready=search_problem is None,
            detail={"index": config.retrieval.index_name},
            problem=search_problem,
        )
    )
    for name, kind, path, _excludes in _fileset_targets(config):
        problem = None
        detail: dict = {"path": str(path) if str(path) != "." else ""}
        if not str(path) or str(path) == ".":
            problem = "no path configured for this deployment"
        elif path.is_dir():
            detail["bytes"] = _directory_bytes(path)
            detail["empty"] = detail["bytes"] == 0
        elif path.parent.is_dir():
            # The store exists, it just has nothing in it yet — nobody has uploaded a file,
            # or no connector has staged one. That is the normal state of a fresh
            # appliance and must not be reported as a fault; the directory is created when
            # there is something to put in it.
            detail["bytes"] = 0
            detail["empty"] = True
        else:
            problem = f"{path} is not a directory (not mounted into this container?)"
        entries.append(
            ComponentPlan(
                name=name,
                kind=kind,
                enabled=True,
                ready=problem is None,
                detail=detail,
                problem=problem,
            )
        )
    config_root = config.artifact_dir.expanduser().parent
    entries.append(
        ComponentPlan(
            name=APPLIANCE_CONFIG,
            kind="files",
            enabled=True,
            ready=config_root.is_dir(),
            detail={"path": str(config_root), "top_level_only": True},
            problem=None if config_root.is_dir() else f"{config_root} is not a directory",
        )
    )
    if config.backup.sources.environment_secrets:
        missing_key = "KI_CONNECTOR_CREDENTIAL_KEY" not in os.environ
        entries.append(
            ComponentPlan(
                name="secrets/environment",
                kind="secrets",
                enabled=True,
                ready=not missing_key,
                detail={"encrypted": config.backup.encrypt},
                problem=(
                    "KI_CONNECTOR_CREDENTIAL_KEY is not set in this container, so the one "
                    "secret a restore cannot do without would not be captured"
                    if missing_key
                    else None
                ),
            )
        )
    return entries


def collect(
    config: AppConfig,
    staging: Path,
    backup_id: str,
    *,
    on_step: Callable[[str], None] | None = None,
    compress: bool = True,
) -> Iterator[tuple[StagedComponent | None, str | None]]:
    """Capture every configured store, yielding ``(component, warning)`` as it goes.

    A generator so the caller can transfer and delete each component before the next one
    is staged, which is what keeps the staging disk requirement at one component rather
    than the whole appliance. An optional store that cannot be captured yields a warning
    and no component; a required one raises.

    ``compress`` is false when the destination deduplicates, and it has to reach every
    capture rather than being applied afterwards: compression is what hides an unchanged
    store from a chunker, so it must not happen at all rather than happen and be undone.
    """
    step = on_step or (lambda _message: None)

    for target in database_targets(config):
        step(f"dumping {target.name}")
        if not target.url:
            yield None, f"postgres/{target.name} was skipped: no URL is configured for it"
            continue
        try:
            yield dump_database(target, staging, compress=compress), None
        except ComponentError as exc:
            if target.required:
                raise
            yield None, f"postgres/{target.name} was skipped: {exc}"

    if config.backup.sources.search_index:
        step("snapshotting the search index")
        try:
            yield snapshot_search_index(config, staging, backup_id), None
        except ComponentError as exc:
            # Derivable from Postgres by re-embedding, so its absence costs money and time
            # rather than data. That is a warning, not a failed backup.
            yield None, f"opensearch/snapshot was skipped: {exc}"

    for name, kind, path, excludes in _fileset_targets(config):
        step(f"archiving {name}")
        if not str(path) or str(path) == ".":
            yield None, f"{name} was skipped: no path is configured for it in this deployment"
            continue
        if not path.is_dir():
            if path.parent.is_dir():
                # Nothing has been put here yet. Captured as an empty archive rather than
                # skipped, so the manifest records that the store was covered and held
                # nothing — which is a different fact from the store being unreachable.
                path.mkdir(parents=True, exist_ok=True)
            else:
                yield None, f"{name} was skipped: {path} is not a directory in this container"
                continue
        try:
            yield (
                archive_directory(
                    name, kind, path, staging, excludes=excludes, compress=compress
                ),
                None,
            )
        except ComponentError as exc:
            yield None, f"{name} was skipped: {exc}"

    step("capturing the appliance's configuration")
    config_root = config.artifact_dir.expanduser().parent
    if config_root.is_dir():
        yield archive_root_files(APPLIANCE_CONFIG, config_root, staging), None
    else:
        yield None, f"{APPLIANCE_CONFIG} was skipped: {config_root} is not a directory"

    if config.backup.sources.environment_secrets:
        step("capturing deployment secrets")
        yield capture_environment_secrets(config, staging), None


# ----------------------------------------------------------------------------- helpers


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _directory_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _http_detail(response: httpx.Response) -> str:
    body = response.text.strip()
    return f"HTTP {response.status_code}: {body[:400]}" if body else f"HTTP {response.status_code}"
