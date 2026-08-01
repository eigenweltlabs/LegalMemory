"""Customer-DMS connector via a plugin drop directory.

The zero-repo-change integration path: a forward-deployed engineer ships a small
standalone script (see ``examples/plugins/reference_export.py``) that reads a
customer DMS — RA-MICRO, AnNoText, Advoware, an in-house SQL export — and writes a
*drop directory* in a versioned schema.  This connector reads that directory; no
connector code lands in this repo per customer.  The full contract lives in
``docs/src/content/docs/development/plugin-connectors.md``.

Drop directory layout::

    <root>/observations.jsonl   one JSON object per line, first field "schema"
    <root>/files/               content bytes, referenced by "content_file"

Fail loudly: a malformed line, an unknown schema marker, or a content path that
escapes ``<root>/files/`` raises rather than half-syncing.  A plugin bug must
never silently drop or truncate a customer's index.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from knowledge_index.sync.base import (
    ChangeBatch,
    SourceCapabilities,
    SourceObjectObservation,
    UnsupportedOperation,
)

SCHEMA_MARKER = "ki-plugin-observation/v1"
OBSERVATIONS_FILE = "observations.jsonl"
FILES_DIR = "files"


class PluginDropSource:
    """Read observations + bytes a customer plugin script maintains in a drop dir."""

    kind = "plugin_drop"

    capabilities = SourceCapabilities(
        delta=False,
        webhooks=False,
        acl=True,
        versions=False,
        stable_ids=True,
        # A directory listing distinguishes empty from unavailable.
        verifiable_emptiness=True,
    )

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        if not candidate.is_dir():
            raise ValueError(f"plugin drop root is not a directory: {candidate}")
        self.root = candidate.resolve(strict=True)
        self.files_root = (self.root / FILES_DIR).resolve()
        self._observations_path = self.root / OBSERVATIONS_FILE
        # external_id -> content_file (relative path under <root>/files/)
        self._content_files: dict[str, str] = {}

    def full_scan(self) -> Iterator[SourceObjectObservation]:
        if not self._observations_path.is_file():
            raise FileNotFoundError(
                f"plugin drop is missing {OBSERVATIONS_FILE}: {self._observations_path} — "
                "the plugin script must (re)generate the drop directory before syncing"
            )
        self._content_files.clear()
        with self._observations_path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                entry = self._parse_line(line, line_number)
                external_id = entry["external_id"]
                # Record the content file for fetch() even for tombstones we skip below,
                # so a re-appearing object still resolves; harmless if never fetched.
                if not entry.get("deleted"):
                    self._content_files[external_id] = entry["content_file"]
                    yield self._observation(entry, line_number)
                else:
                    # deleted:true is an explicit tombstone. Do not yield: the engine's
                    # full-scan diffing tombstones anything absent from the emitted set,
                    # so an explicit delete and a plain absence behave identically.
                    self._content_files.pop(external_id, None)

    def changes(self, cursor: str | None) -> ChangeBatch:
        del cursor
        raise UnsupportedOperation(
            "plugin drop directories are snapshots; the engine falls back to full_scan diffing"
        )

    def fetch(self, external_id: str) -> BinaryIO:
        content_file = self._content_files.get(external_id)
        if content_file is None:
            # fetch() may run in a worker/process after the scan transaction committed.
            for _ in self.full_scan():
                if external_id in self._content_files:
                    break
            content_file = self._content_files.get(external_id)
        if content_file is None:
            raise FileNotFoundError(external_id)
        path = self._safe_content_path(content_file)
        if not path.is_file():
            raise FileNotFoundError(content_file)
        return path.open("rb")

    def _safe_content_path(self, content_file: str) -> Path:
        relative = Path(content_file)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"content_file must be a safe relative path: {content_file!r}")
        unresolved = self.files_root / relative
        if unresolved.is_symlink():
            raise ValueError(f"content_file resolves through a symlink: {content_file!r}")
        candidate = unresolved.resolve()
        if not candidate.is_relative_to(self.files_root):
            raise ValueError(f"content_file escapes the drop files directory: {content_file!r}")
        return candidate

    def _parse_line(self, line: str, line_number: int) -> dict:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{OBSERVATIONS_FILE} line {line_number}: invalid JSON ({exc})") from exc
        if not isinstance(entry, dict):
            raise ValueError(f"{OBSERVATIONS_FILE} line {line_number}: expected a JSON object")

        schema = entry.get("schema")
        if schema != SCHEMA_MARKER:
            raise ValueError(
                f"{OBSERVATIONS_FILE} line {line_number}: unexpected schema {schema!r}; "
                f"expected {SCHEMA_MARKER!r} — regenerate the drop directory with a plugin "
                "that targets this connector version"
            )

        for required in ("external_id", "path", "name"):
            value = entry.get(required)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"{OBSERVATIONS_FILE} line {line_number}: {required!r} is required and "
                    "must be a non-empty string"
                )
        if not entry.get("deleted") and not isinstance(entry.get("content_file"), str):
            raise ValueError(
                f"{OBSERVATIONS_FILE} line {line_number}: 'content_file' is required for "
                "non-deleted entries and must be a string"
            )
        acl = entry.get("acl")
        if acl is not None and not isinstance(acl, list):
            raise ValueError(
                f"{OBSERVATIONS_FILE} line {line_number}: 'acl' must be a list of grants or absent"
            )
        return entry

    def _observation(self, entry: dict, line_number: int) -> SourceObjectObservation:
        size = entry.get("size_bytes")
        if size is not None and not isinstance(size, int):
            raise ValueError(
                f"{OBSERVATIONS_FILE} line {line_number}: 'size_bytes' must be an integer or absent"
            )
        return SourceObjectObservation(
            external_id=entry["external_id"],
            path=entry["path"],
            name=entry["name"],
            mime_type=entry.get("mime_type"),
            size_bytes=size,
            mtime=_parse_mtime(entry.get("mtime"), line_number),
            # ACL passthrough: absent (None) means unknown -> engine policy applies, which is
            # deny-by-default for external connectors (see permissions.version_predicate).
            acl=entry.get("acl"),
        )


def _parse_mtime(value: object, line_number: int) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{OBSERVATIONS_FILE} line {line_number}: 'mtime' must be an ISO-8601 string or absent"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{OBSERVATIONS_FILE} line {line_number}: 'mtime' is not ISO-8601: {value!r} ({exc})"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
