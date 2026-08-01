"""Where backups are written, behind one narrow interface.

Three implementations, because a firm running this appliance has three realistic answers.
``LocalDestination`` writes to a directory, which in practice is a mounted NAS or SMB
share — the copy that is on different hardware in the same building. ``S3Destination``
writes to any S3-compatible endpoint, which is the copy that is somewhere else entirely.
Both store whole objects: every night is a complete copy that shares nothing with the
night before, which is what makes each backup directory readable with ``sha256sum -c`` and
restorable with nothing but tar and pg_restore.

``ResticDestination`` (in :mod:`knowledge_index.backup.restic`) makes the opposite trade
and is the one to reach for at scale: a night is stored as the difference from the night
before, so a hundred thousand documents are transferred once rather than every evening.
The price is that a backup is no longer a directory a human can read without restic.

The interface is deliberately smaller than any of them can do: write a stream, read a
stream, list backup ids, delete a backup, and prove you can do all of that before a run
starts. Everything above this module — manifests, retention, verification — is identical
whichever backend is configured, which is the point. A firm that moves from a NAS to MinIO
to restic changes one setting and keeps every guarantee.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from knowledge_index.config import BackupDestinationConfig

# Written and read back during preflight. Small, uniquely named, and removed afterwards:
# the only honest way to answer "can this appliance actually write here" is to write here.
PROBE_NAME = ".ki-backup-probe"
_COPY_BYTES = 1024 * 1024


class DestinationError(RuntimeError):
    """The destination is unreachable, unwritable, or misconfigured."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    bytes_written: int
    sha256: str


class Destination(Protocol):
    """The whole contract. Backends implement exactly this.

    The three capability flags are how a backend says what it does for itself, so nothing
    above this module has to know which backend is configured. A store-whole-objects
    backend leaves all three false and gets the appliance's own encryption and
    compression; a deduplicating one takes plaintext, uncompressed, and does both itself —
    because pre-compressed ciphertext shares no chunks with anything and would defeat the
    only reason to use it.
    """

    # Encrypts what it stores, so the appliance must not seal components first.
    provides_encryption: bool = False
    # Wants its input uncompressed, so its chunker can find what did not change.
    prefers_uncompressed: bool = False
    # Stores a night as the difference from the night before.
    deduplicates: bool = False

    def write(self, backup_id: str, key: str, reader: BinaryIO) -> StoredObject: ...

    @contextmanager
    def open(self, backup_id: str, key: str) -> Iterator[BinaryIO]: ...

    def exists(self, backup_id: str, key: str) -> bool: ...

    def list_backups(self) -> list[str]: ...

    def delete_backup(self, backup_id: str) -> int: ...

    def check_writable(self) -> None: ...

    def describe(self) -> dict: ...


class _HashingReader:
    """Digests a stream as something else consumes it.

    The checksum has to be over the bytes that actually reached the destination, not over
    a second read of the source — a source that changes between the two reads would
    produce a manifest that certifies a file nobody ever wrote.
    """

    def __init__(self, wrapped: BinaryIO) -> None:
        self._wrapped = wrapped
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._wrapped.read(size)
        self._digest.update(chunk)
        self.bytes_read += len(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def build_destination(
    config: BackupDestinationConfig, secrets: dict[str, str | None] | None = None
) -> Destination:
    """Build the configured backend.

    ``secrets`` carries values an administrator typed into the admin UI, already resolved
    against the environment. Passed in rather than read here, because this module has no
    database session and the resolution order — environment first, then what was saved —
    belongs in one place.
    """
    resolved = {name: value for name, value in (secrets or {}).items() if value}
    if config.kind == "local":
        return LocalDestination(config)
    if config.kind == "s3":
        return S3Destination(config, resolved)
    if config.kind == "restic":
        # Imported here so a deployment that does not use restic never needs the module,
        # and an unset password is reported when a backup runs rather than at import.
        from knowledge_index.backup.restic import ResticDestination

        return ResticDestination(config, resolved)
    raise DestinationError(f"unknown backup destination kind: {config.kind!r}")


class LocalDestination:
    """A directory, which is almost always a mount.

    Stores whole objects: tonight's dump lands beside last night's and shares nothing with
    it. That is the trade for a backup directory a human can read with ``sha256sum -c`` and
    restore with nothing but tar and pg_restore.

    Writes go to a temporary file in the same directory and are fsynced and renamed into
    place, so an interrupted run leaves no half-file that a later listing would count as a
    component. The directory is fsynced too: on most filesystems the rename is what makes
    the file durable, and without that a power cut can leave a backup whose manifest
    survived and whose components did not.
    """

    provides_encryption = False
    prefers_uncompressed = False
    deduplicates = False

    def __init__(self, config: BackupDestinationConfig) -> None:
        self._config = config
        if not str(config.path).strip():
            raise DestinationError("backup destination path is empty")
        self._root = Path(config.path).expanduser()
        if config.prefix.strip():
            self._root = self._root / config.prefix.strip().strip("/")

    def _dir(self, backup_id: str) -> Path:
        return self._root / backup_id

    def _path(self, backup_id: str, key: str) -> Path:
        target = (self._dir(backup_id) / key).resolve()
        root = self._root.resolve()
        # A component key comes out of a manifest, and a manifest can come off a NAS. A
        # key of "../../etc/whatever" must not be able to make a restore write outside the
        # backup directory it came from.
        if root != target and root not in target.parents:
            raise DestinationError(f"component key escapes the backup directory: {key!r}")
        return target

    def write(self, backup_id: str, key: str, reader: BinaryIO) -> StoredObject:
        target = self._path(backup_id, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        hashing = _HashingReader(reader)
        handle, temp_name = tempfile.mkstemp(prefix=".partial-", dir=str(target.parent))
        try:
            with os.fdopen(handle, "wb") as sink:
                shutil.copyfileobj(hashing, sink, _COPY_BYTES)
                sink.flush()
                os.fsync(sink.fileno())
            Path(temp_name).replace(target)
            _fsync_directory(target.parent)
        except Exception as exc:
            Path(temp_name).unlink(missing_ok=True)
            raise DestinationError(f"could not write {key} to {target.parent}: {exc}") from exc
        return StoredObject(key=key, bytes_written=hashing.bytes_read, sha256=hashing.hexdigest())

    @contextmanager
    def open(self, backup_id: str, key: str) -> Iterator[BinaryIO]:
        path = self._path(backup_id, key)
        if not path.is_file():
            raise DestinationError(f"backup component is missing: {path}")
        with path.open("rb") as handle:
            yield handle

    def exists(self, backup_id: str, key: str) -> bool:
        return self._path(backup_id, key).is_file()

    def list_backups(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(entry.name for entry in self._root.iterdir() if entry.is_dir())

    def delete_backup(self, backup_id: str) -> int:
        directory = self._dir(backup_id)
        if not directory.is_dir():
            return 0
        removed = sum(1 for _ in directory.rglob("*") if _.is_file())
        shutil.rmtree(directory)
        return removed

    def check_writable(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise DestinationError(f"backup destination {self._root} cannot be created: {exc}")
        probe = self._root / f"{PROBE_NAME}-{os.getpid()}"
        try:
            probe.write_bytes(b"knowledge-index backup destination probe\n")
            probe.read_bytes()
        except Exception as exc:
            raise DestinationError(
                f"backup destination {self._root} is not writable: {exc}. If this is a "
                "mount, check that it is mounted read-write inside the container."
            ) from exc
        finally:
            probe.unlink(missing_ok=True)

    def describe(self) -> dict:
        usage = shutil.disk_usage(self._root) if self._root.is_dir() else None
        return {
            "kind": "local",
            "location": str(self._root),
            "free_bytes": usage.free if usage else None,
            "total_bytes": usage.total if usage else None,
        }


class S3Destination:
    """Any S3-compatible endpoint: MinIO on the firm's hardware, Wasabi, AWS.

    boto3 is an optional dependency (``pip install 'knowledge-index[s3]'``) and imported
    lazily, so an air-gapped install that backs up to a NAS never needs it and never has
    to explain why an AWS SDK is in its image.
    """

    provides_encryption = False
    prefers_uncompressed = False
    deduplicates = False

    def __init__(
        self, config: BackupDestinationConfig, secrets: dict[str, str] | None = None
    ) -> None:
        self._config = config
        self._secrets = secrets or {}
        if not config.bucket.strip():
            raise DestinationError("backup destination kind is s3 but no bucket is configured")
        self._bucket = config.bucket.strip()
        self._prefix = config.prefix.strip().strip("/")
        self._client = None

    def _key(self, backup_id: str, key: str) -> str:
        parts = [part for part in (self._prefix, backup_id, key) if part]
        return "/".join(parts)

    def _connect(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            raise DestinationError(
                "the s3 backup destination needs boto3, which is not installed. Install it "
                "with `pip install 'knowledge-index[s3]'`, or set the destination kind to "
                "'local' and back up to a mounted share."
            ) from exc
        access_key = self._secrets.get("s3_access_key_id", "")
        secret_key = self._secrets.get("s3_secret_access_key", "")
        if not access_key or not secret_key:
            # An empty pair is not "fall back to the instance role" here: this appliance
            # runs on the firm's own hardware, so silent anonymous access would just fail
            # later with a 403 that says nothing about which variable is missing.
            raise DestinationError(
                "the S3 access key and secret key are both needed for this destination. "
                "Set them under Backup \u2192 Destination."
            )
        self._client = boto3.client(
            "s3",
            endpoint_url=self._config.endpoint_url.strip() or None,
            region_name=self._config.region or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(
                s3={"addressing_style": "path" if self._config.use_path_style else "virtual"},
                retries={"max_attempts": 5, "mode": "standard"},
            ),
        )
        return self._client

    def write(self, backup_id: str, key: str, reader: BinaryIO) -> StoredObject:
        client = self._connect()
        hashing = _HashingReader(reader)
        try:
            # upload_fileobj chooses multipart on its own above the threshold, so a
            # 200 GB component streams rather than being buffered.
            client.upload_fileobj(hashing, self._bucket, self._key(backup_id, key))
        except Exception as exc:
            raise DestinationError(f"could not upload {key} to s3://{self._bucket}: {exc}") from exc
        return StoredObject(key=key, bytes_written=hashing.bytes_read, sha256=hashing.hexdigest())

    @contextmanager
    def open(self, backup_id: str, key: str) -> Iterator[BinaryIO]:
        client = self._connect()
        try:
            response = client.get_object(Bucket=self._bucket, Key=self._key(backup_id, key))
        except Exception as exc:
            raise DestinationError(
                f"backup component is missing or unreadable: s3://{self._bucket}/"
                f"{self._key(backup_id, key)}: {exc}"
            ) from exc
        body = response["Body"]
        try:
            yield body
        finally:
            body.close()

    def exists(self, backup_id: str, key: str) -> bool:
        client = self._connect()
        try:
            client.head_object(Bucket=self._bucket, Key=self._key(backup_id, key))
            return True
        except Exception:
            return False

    def list_backups(self) -> list[str]:
        client = self._connect()
        prefix = f"{self._prefix}/" if self._prefix else ""
        found: list[str] = []
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix, Delimiter="/"):
                for entry in page.get("CommonPrefixes", []):
                    name = entry["Prefix"][len(prefix) :].strip("/")
                    if name:
                        found.append(name)
        except Exception as exc:
            raise DestinationError(f"could not list s3://{self._bucket}/{prefix}: {exc}") from exc
        return sorted(found)

    def delete_backup(self, backup_id: str) -> int:
        client = self._connect()
        prefix = self._key(backup_id, "")
        removed = 0
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if not keys:
                    continue
                client.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})
                removed += len(keys)
        except Exception as exc:
            raise DestinationError(f"could not delete {backup_id} from s3: {exc}") from exc
        return removed

    def check_writable(self) -> None:
        client = self._connect()
        key = self._key(PROBE_NAME, f"probe-{os.getpid()}")
        try:
            client.put_object(Bucket=self._bucket, Key=key, Body=b"knowledge-index probe\n")
            client.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise DestinationError(
                f"s3://{self._bucket} is not writable with the configured credentials: {exc}"
            ) from exc
        finally:
            try:
                client.delete_object(Bucket=self._bucket, Key=key)
            except Exception:  # noqa: BLE001 - a leftover probe object is not worth failing over
                pass

    def describe(self) -> dict:
        return {
            "kind": "s3",
            "location": f"s3://{self._bucket}/{self._prefix}".rstrip("/"),
            "endpoint_url": self._config.endpoint_url or "(aws)",
            "region": self._config.region,
            "free_bytes": None,
            "total_bytes": None,
        }


def _fsync_directory(path: Path) -> None:
    """Make a rename durable. Not every filesystem needs it; the ones that do, need it."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:  # noqa: BLE001 - SMB and some network mounts do not implement it
        pass
    finally:
        os.close(fd)
