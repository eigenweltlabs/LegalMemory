"""A restic repository as a backup destination, which is what makes backups incremental.

Every other destination here stores whole objects: a night's ``postgres-ki.dump`` lands
beside last night's, and the two share nothing even where the databases were identical.
For a firm holding a hundred thousand documents that is the whole estate transferred and
stored again every night, times whatever retention keeps — nineteen full copies under the
default rules.

restic stores content-defined chunks instead. A dump that differs from yesterday's in a
few thousand rows shares almost every chunk with it, so the second night costs the delta
rather than the estate. The same property covers the artifact blob store, which is the
largest thing here and the one that changes least.

Two consequences shape how the rest of this package talks to it, and both are declared on
the class rather than special-cased by the caller:

``provides_encryption``
    restic encrypts every chunk under its own key and authenticates it. Sealing a
    component with :mod:`knowledge_index.backup.crypto` first would hand restic
    high-entropy ciphertext that shares no chunks with anything, which is exactly the
    property deduplication needs and encryption destroys. So this destination takes
    plaintext and owns the encryption itself, under the same operator-supplied key.

``prefers_uncompressed``
    for the same reason. ``pg_dump --compress=6`` and ``tar czf`` both turn a small change
    to their input into a completely different output stream. restic compresses what it
    stores anyway, after chunking, so the compression is not lost — it just happens where
    it does not defeat the chunker.

Snapshots carry three tags: ``ki`` marks them as ours, ``backup:<id>`` groups the
components of one backup, and ``key:<component key>`` names which component it is. The
manifest is stored the same way, so a backup is complete in the repository or it is not
there at all, and everything above this module — manifests, retention, verification —
works unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from knowledge_index.config import BackupDestinationConfig

# restic gained content-defined compression, and with it repository format 2, in 0.14.
# Below that a repository cannot compress what it dedupes, which is most of the point.
MINIMUM_VERSION = (0, 14, 0)
_COPY_BYTES = 1024 * 1024
_TAG = "ki"
_COMMAND_TIMEOUT = float(os.environ.get("KI_BACKUP_RESTIC_TIMEOUT_SECONDS", "86400"))


def _log(message: str) -> None:
    print(f"[ki backup] {message}", file=sys.stderr, flush=True)


class ResticError(RuntimeError):
    """restic is missing, too old, or refused a command."""


class ResticDestination:
    """A restic repository, local or S3-compatible, driven through the restic binary.

    Driven as a subprocess rather than reimplemented: the repository format, the chunker
    and the crypto are restic's, and the value of using it at all is that they are its
    responsibility and not this appliance's.
    """

    provides_encryption = True
    prefers_uncompressed = True
    deduplicates = True

    def __init__(
        self, config: BackupDestinationConfig, secrets: dict[str, str] | None = None
    ) -> None:
        self._config = config
        self._secrets = secrets or {}
        self._repository = _repository_url(config)
        self._initialized = False

    # ------------------------------------------------------------------ process plumbing

    def _environment(self) -> dict:
        password = self._secrets.get("encryption_key", "")
        if not password:
            raise ResticError(
                "No backup key is set, so the repository cannot be opened. Open "
                "Backup \u2192 Security and press Generate. Keep the copy it shows you "
                "somewhere off this machine: a repository whose key is lost cannot be read "
                "by anyone, including us."
            )
        environment = dict(os.environ)
        environment["RESTIC_REPOSITORY"] = self._repository
        environment["RESTIC_PASSWORD"] = password
        if self._config.kind == "restic" and self._repository.startswith("s3:"):
            access = self._secrets.get("s3_access_key_id", "")
            secret = self._secrets.get("s3_secret_access_key", "")
            if not access or not secret:
                raise ResticError(
                    "the S3 access key and secret key are both needed for a repository in "
                    "object storage. Set them under Backup \u2192 Destination."
                )
            environment["AWS_ACCESS_KEY_ID"] = access
            environment["AWS_SECRET_ACCESS_KEY"] = secret
            if self._config.region:
                environment["AWS_DEFAULT_REGION"] = self._config.region
        return environment

    def _command(self, *arguments: str) -> list[str]:
        """``restic`` plus the flags every invocation takes, then the subcommand.

        ``--no-cache`` is offered because restic's cache is per-repository local state, and
        a container that is recreated nightly rebuilds it on every run for no benefit.
        """
        flags = ["--quiet"]
        if self._config.restic_no_cache:
            flags.append("--no-cache")
        return ["restic", *flags, *arguments]

    def _run(self, *arguments: str, check: bool = True, stdin=None) -> subprocess.CompletedProcess:
        command = self._command(*arguments)
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                command,
                capture_output=True,
                text=True,
                env=self._environment(),
                timeout=_COMMAND_TIMEOUT,
                stdin=stdin,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ResticError(
                "restic is not installed in this image, so the restic backup destination "
                "cannot be used. It is installed in the app and worker images this project "
                "ships; a custom image needs the restic package, at least "
                f"{'.'.join(str(part) for part in MINIMUM_VERSION)}."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ResticError(
                f"restic {arguments[0] if arguments else ''} exceeded "
                f"{_COMMAND_TIMEOUT:.0f}s. Raise KI_BACKUP_RESTIC_TIMEOUT_SECONDS if the "
                "repository is genuinely that large or that far away."
            ) from exc
        if check and completed.returncode != 0:
            raise ResticError(
                f"restic {arguments[0] if arguments else ''} failed "
                f"({completed.returncode}): {_last_line(completed.stderr or completed.stdout)}"
            )
        return completed

    # ---------------------------------------------------------------------- the contract

    def check_writable(self) -> None:
        """Make sure the repository exists, is the right format, and answers.

        Creating it here rather than asking an operator to run ``restic init`` by hand: the
        password is already configured, and a first backup that fails because nobody ran a
        setup command is a failure this can simply not have.
        """
        self._require_version()
        probe = self._run("cat", "config", check=False)
        if probe.returncode == 0:
            self._initialized = True
            return
        _log(f"initializing a new restic repository at {_redact(self._repository)}")
        # Format 2 so the repository compresses as well as dedupes. Without it every
        # component is stored raw and a firm pays for the compression it thought it had.
        self._run("init", "--repository-version", "2")
        self._initialized = True

    def _require_version(self) -> None:
        completed = self._run("version", check=False)
        if completed.returncode != 0:
            raise ResticError(
                f"restic did not run: {_last_line(completed.stderr or completed.stdout)}"
            )
        found = _parse_version(completed.stdout)
        if found and found < MINIMUM_VERSION:
            raise ResticError(
                f"restic {'.'.join(str(p) for p in found)} is too old for this destination; "
                f"{'.'.join(str(p) for p in MINIMUM_VERSION)} is the first release whose "
                "repository format compresses what it deduplicates. Upgrade restic, or set "
                "the destination kind to 'local' or 's3'."
            )

    def write(self, backup_id: str, key: str, reader: BinaryIO):
        """Stream one component into the repository as its own tagged snapshot."""
        from knowledge_index.backup.destinations import StoredObject, _HashingReader

        self._ensure_initialized()
        hashing = _HashingReader(reader)
        command = self._command(
            "backup",
            "--stdin",
            "--stdin-filename",
            _snapshot_path(key),
            "--host",
            self._config.restic_host,
            "--tag",
            _TAG,
            "--tag",
            f"backup:{backup_id}",
            "--tag",
            f"key:{key}",
        )
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(),
        )
        try:
            shutil.copyfileobj(hashing, process.stdin, _COPY_BYTES)
            process.stdin.close()
        except BrokenPipeError:
            pass  # restic died; communicate() below reports why
        finally:
            # communicate() flushes stdin if it still holds it, and flushing the handle we
            # just closed raises over the top of whatever actually went wrong.
            process.stdin = None
        _stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT)
        if process.returncode != 0:
            raise ResticError(
                f"restic backup of {key} failed ({process.returncode}): {_last_line(stderr)}"
            )
        return StoredObject(key=key, bytes_written=hashing.bytes_read, sha256=hashing.hexdigest())

    @contextmanager
    def open(self, backup_id: str, key: str) -> Iterator[BinaryIO]:
        """Read one component back out, streamed rather than materialized."""
        self._ensure_initialized()
        snapshot = self._snapshot_id(backup_id, key)
        if snapshot is None:
            raise ResticError(
                f"backup component is missing from the restic repository: {backup_id}/{key}"
            )
        command = self._command("dump", snapshot, _snapshot_path(key))
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(),
        )
        try:
            yield process.stdout
        finally:
            # Whatever the reader did, do not leave restic holding a pipe nobody drains.
            if process.poll() is None:
                process.stdout.close()
                process.terminate()
            stderr = process.stderr.read().decode("utf-8", "replace")
            process.stderr.close()
            process.wait(timeout=60)
            if process.returncode not in (0, None) and process.returncode > 0 and stderr:
                _log(f"restic dump of {backup_id}/{key} ended with: {_last_line(stderr)}")

    def exists(self, backup_id: str, key: str) -> bool:
        return self._snapshot_id(backup_id, key) is not None

    def list_backups(self) -> list[str]:
        found = {
            tag.split(":", 1)[1]
            for snapshot in self._snapshots()
            for tag in snapshot.get("tags") or []
            if tag.startswith("backup:")
        }
        return sorted(found)

    def delete_backup(self, backup_id: str) -> int:
        """Forget every snapshot of one backup. Space is reclaimed by :meth:`prune`.

        Forgetting and pruning are separated because pruning rewrites the repository's pack
        files, and doing that once after a retention pass costs a fraction of doing it after
        each backup that pass removes.
        """
        self._ensure_initialized()
        snapshots = [
            snapshot["id"]
            for snapshot in self._snapshots()
            if f"backup:{backup_id}" in (snapshot.get("tags") or [])
        ]
        if not snapshots:
            return 0
        self._run("forget", *snapshots)
        return len(snapshots)

    def prune(self) -> None:
        """Reclaim the space forgotten snapshots were holding. Safe to call with nothing to do."""
        self._ensure_initialized()
        self._run("prune")

    def describe(self) -> dict:
        detail = {
            "kind": "restic",
            "location": _redact(self._repository),
            "deduplicated": True,
            "encrypted_by_destination": True,
            "free_bytes": None,
            "total_bytes": None,
        }
        if self._config.kind == "restic" and not self._repository.startswith("s3:"):
            path = Path(self._repository)
            if path.is_dir():
                usage = shutil.disk_usage(path)
                detail["free_bytes"] = usage.free
                detail["total_bytes"] = usage.total
        stats = self._run("stats", "--mode", "raw-data", "--json", check=False)
        if stats.returncode == 0:
            try:
                payload = json.loads(stats.stdout)
                # What the repository actually occupies, after dedup and compression —
                # the number that answers "is nightly retention affordable".
                detail["repository_bytes"] = payload.get("total_size")
                detail["blob_count"] = payload.get("total_blob_count")
            except ValueError:
                pass
        return detail

    # ---------------------------------------------------------------------------- helpers

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.check_writable()

    def _snapshots(self) -> list[dict]:
        completed = self._run("snapshots", "--json", "--tag", _TAG, check=False)
        if completed.returncode != 0:
            # An empty repository answers with an error on some versions; either way there
            # is nothing in it, which is not a failure to list.
            return []
        try:
            payload = json.loads(completed.stdout or "[]")
        except ValueError:
            return []
        return payload if isinstance(payload, list) else []

    def _snapshot_id(self, backup_id: str, key: str) -> str | None:
        wanted = {f"backup:{backup_id}", f"key:{key}"}
        matches = [
            snapshot
            for snapshot in self._snapshots()
            if wanted.issubset(set(snapshot.get("tags") or []))
        ]
        if not matches:
            return None
        # Newest wins: a component rewritten by a re-run of the same backup id leaves the
        # older snapshot behind until retention forgets it.
        matches.sort(key=lambda snapshot: snapshot.get("time") or "")
        return matches[-1]["id"]


def _repository_url(config: BackupDestinationConfig) -> str:
    if config.restic_repository.strip():
        return config.restic_repository.strip()
    if config.bucket.strip():
        endpoint = config.endpoint_url.strip().removeprefix("https://").removeprefix("http://")
        prefix = config.prefix.strip().strip("/")
        bucket = config.bucket.strip()
        location = f"{endpoint}/{bucket}" if endpoint else bucket
        return f"s3:{location}/{prefix}" if prefix else f"s3:{location}"
    if not config.path.strip():
        raise ResticError(
            "the restic destination needs somewhere to put the repository: set a path, a "
            "bucket, or an explicit restic repository string"
        )
    root = Path(config.path).expanduser()
    prefix = config.prefix.strip().strip("/")
    return str(root / prefix if prefix else root)


def _snapshot_path(key: str) -> str:
    """The path a component takes inside its snapshot.

    Absolute and normalized, because ``--stdin-filename`` is a path and a key arriving
    from a manifest is not this appliance's to trust.
    """
    cleaned = "/".join(part for part in key.split("/") if part and part not in (".", ".."))
    return f"/{cleaned}"


def _parse_version(text: str) -> tuple[int, ...] | None:
    for token in (text or "").split():
        parts = token.split(".")
        if len(parts) >= 2 and all(part.isdigit() for part in parts[:2]):
            return tuple(int(part) for part in parts if part.isdigit())
    return None


def _redact(repository: str) -> str:
    """A repository string safe to show an operator: no embedded credentials."""
    if "@" in repository and "://" not in repository:
        return repository.split("@", 1)[-1]
    return repository


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""
