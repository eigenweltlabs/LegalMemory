"""Reference read-only connector for a local or mounted filesystem tree."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from knowledge_index.sync.base import (
    ChangeBatch,
    SourceCapabilities,
    SourceObjectObservation,
    UnsupportedOperation,
)

AclResolver = Callable[[Path], list[dict] | None]


class LocalFilesystemSource:
    """Enumerate and fetch files below one fixed root without following symlinks.

    Relative POSIX paths are external ids. Consequently a rename is represented as a
    tombstone plus a new object; later content hashing reconnects both observations to
    the same Blob. This is honest about what a portable filesystem connector can know.
    """

    kind = "local_fs"

    def __init__(
        self,
        root: str | Path,
        *,
        acl_resolver: AclResolver | None = None,
        include_hidden: bool = True,
    ) -> None:
        candidate = Path(root).expanduser()
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"local source root is not a directory: {candidate}")
        self.root = candidate.resolve(strict=True)
        self._acl_resolver = acl_resolver
        self._include_hidden = include_hidden
        self.capabilities = SourceCapabilities(
            delta=False,
            webhooks=False,
            acl=acl_resolver is not None,
            versions=False,
            stable_ids=False,
            # A directory listing distinguishes empty from unavailable.
            verifiable_emptiness=True,
        )

    def full_scan(self) -> Iterator[SourceObjectObservation]:
        for path in sorted(self.root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(self.root)
            if not self._include_hidden and any(part.startswith(".") for part in relative.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            external_id = PurePosixPath(*relative.parts).as_posix()
            yield SourceObjectObservation(
                external_id=external_id,
                path=external_id,
                name=path.name,
                mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                size_bytes=stat.st_size,
                mtime=_datetime_from_timestamp(stat.st_mtime),
                change_hint=f"{stat.st_mtime_ns}:{stat.st_size}",
                acl=self._acl_resolver(path) if self._acl_resolver else None,
            )

    def changes(self, cursor: str | None) -> ChangeBatch:
        raise UnsupportedOperation("portable filesystems have no reliable delta cursor")

    def fetch(self, external_id: str) -> BinaryIO:
        path = self._safe_path(external_id)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(external_id)
        return path.open("rb")

    def _safe_path(self, external_id: str) -> Path:
        relative = Path(external_id)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("external id must be a safe relative path")
        unresolved = self.root / relative
        if unresolved.is_symlink():
            raise FileNotFoundError(external_id)
        candidate = unresolved.resolve(strict=True)
        if not candidate.is_relative_to(self.root):
            raise ValueError("external id escapes the configured source root")
        return candidate


def _datetime_from_timestamp(value: float):
    # Imported lazily in one place to keep the observation constructor readable.
    from datetime import UTC, datetime

    return datetime.fromtimestamp(value, tz=UTC)
