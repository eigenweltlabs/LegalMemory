"""Content-addressed local artifact store used by the single-VM deployment.

Also the one place that takes bytes *away* again. Deletion matters as much as storage
here: an administrator who disconnects a source is telling the firm that client's
documents are gone, so the copies this module staged under the artifact volume have to
go with the connection. Content addressing makes that a reference-counting problem
rather than a directory removal — the same bytes in two matters are one blob, and the
blob has to outlive every deletion except the last one.

Reclamation never uses ``ignore_errors``. A file that could not be unlinked is the exact
fact an operator needs; swallowing it would leave the appliance asserting a deletion it
did not perform.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


class ArtifactTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class StoredBlob:
    content_hash: str
    size_bytes: int
    path: Path


@dataclass
class ReclaimReport:
    """What a deletion actually reclaimed, and what it could not.

    ``failures`` is the load-bearing field: it is non-empty exactly when the database
    now says something is gone while bytes for it are still on the volume.
    """

    files_removed: int = 0
    bytes_reclaimed: int = 0
    blobs_removed: int = 0
    blobs_retained_shared: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures

    def absorb(self, other: "ReclaimReport") -> "ReclaimReport":
        self.files_removed += other.files_removed
        self.bytes_reclaimed += other.bytes_reclaimed
        self.blobs_removed += other.blobs_removed
        self.blobs_retained_shared += other.blobs_retained_shared
        self.failures.extend(other.failures)
        return self

    def payload(self) -> dict:
        return {
            "files_removed": self.files_removed,
            "bytes_reclaimed": self.bytes_reclaimed,
            "blobs_removed": self.blobs_removed,
            "blobs_retained_shared": self.blobs_retained_shared,
            "complete": self.complete,
            "failures": list(self.failures),
        }


def measure_tree(root: Path) -> tuple[int, int]:
    """Count the files under ``root`` and the bytes they occupy. Missing root = (0, 0)."""
    files = 0
    total = 0
    if not root.exists():
        return files, total
    if root.is_file() and not root.is_symlink():
        return 1, root.stat().st_size
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            # Counted anyway: a file we cannot stat is still a file we must account for.
            files += 1
            continue
        files += 1
    return files, total


def remove_tree(root: Path) -> ReclaimReport:
    """Delete a directory tree and report exactly what went and what stayed.

    Measured before and after rather than trusting the removal call, so a partially
    failed delete reports the bytes it really reclaimed instead of the bytes it intended
    to.
    """
    report = ReclaimReport()
    if not root.exists():
        return report
    before_files, before_bytes = measure_tree(root)
    # Kept apart because a directory that will not go is almost always a *consequence* of
    # the file inside it that would not go, and reporting the consequence buries the
    # cause an operator has to act on.
    file_problems: list[str] = []
    directory_problems: list[str] = []
    # Walked by hand rather than shutil.rmtree so that one unremovable file does not
    # abort the walk and leave the rest of a client's documents in place.
    for directory, subdirectories, filenames in os.walk(root, topdown=False):
        for name in filenames:
            try:
                os.unlink(os.path.join(directory, name))
            except OSError as exc:
                file_problems.append(f"{os.path.join(directory, name)}: {exc}")
        for name in subdirectories:
            path = os.path.join(directory, name)
            try:
                if os.path.islink(path):
                    os.unlink(path)
                else:
                    os.rmdir(path)
            except OSError as exc:
                directory_problems.append(f"{path}: {exc}")
    try:
        os.rmdir(root)
    except OSError as exc:
        directory_problems.append(f"{root}: {exc}")
    after_files, after_bytes = measure_tree(root)
    report.files_removed = before_files - after_files
    report.bytes_reclaimed = before_bytes - after_bytes
    if after_files or root.exists():
        problems = file_problems or directory_problems
        detail = "; ".join(problems[:5]) if problems else "no error reported"
        if len(problems) > 5:
            detail += f"; and {len(problems) - 5} more"
        report.failures.append(
            f"{root}: {after_files} file(s) and {after_bytes} byte(s) remain ({detail})"
        )
    return report


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.blob_root = self.root / "blobs"
        self.temp_root = self.root / "tmp"
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def put_blob(self, stream: BinaryIO, *, max_bytes: int) -> StoredBlob:
        digest = hashlib.sha256()
        size = 0
        fd, temp_name = tempfile.mkstemp(prefix="ingest-", dir=self.temp_root)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as target:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ArtifactTooLarge(
                            f"document exceeds configured {max_bytes} byte limit"
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            content_hash = digest.hexdigest()
            destination = self.path_for_hash(content_hash)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temp_path.unlink()
            else:
                temp_path.replace(destination)
            return StoredBlob(content_hash=content_hash, size_bytes=size, path=destination)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def path_for_hash(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise ValueError("invalid SHA-256 content hash")
        return self.blob_root / content_hash[:2] / content_hash[2:4] / content_hash

    def remove_blob(self, content_hash: str) -> int:
        """Unlink one blob and return the bytes it occupied. Zero means it was not there.

        The caller decides whether the blob still has a referent; this method only obeys.
        Raises ``OSError`` when the file exists and cannot be removed — that is the case
        an operator has to hear about, so it is never swallowed here.
        """
        path = self.path_for_hash(content_hash)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0
        path.unlink()
        # Fan-out directories are pure sharding, so an empty one is noise that
        # accumulates one per deleted document. Failure to prune is not a failure to
        # reclaim: the bytes are already gone.
        for parent in (path.parent, path.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
        return size

    def reclaim_blobs(self, content_hashes: Iterable[str]) -> ReclaimReport:
        """Unlink every named blob, continuing past the ones that refuse to go."""
        report = ReclaimReport()
        for content_hash in content_hashes:
            try:
                freed = self.remove_blob(content_hash)
            except (OSError, ValueError) as exc:
                report.failures.append(f"blob {content_hash}: {exc}")
                continue
            if freed:
                report.files_removed += 1
                report.bytes_reclaimed += freed
            report.blobs_removed += 1
        return report
