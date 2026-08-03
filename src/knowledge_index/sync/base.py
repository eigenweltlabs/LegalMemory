"""SyncSource protocol — the contract every connector implements.

Connectors are read-only and dumb on purpose: enumerate, fetch, report ACLs.
All intelligence (scheduling, diffing, checkpoints, tombstones, retries) lives in
the engine. See docs/architecture.md §1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO, Protocol, runtime_checkable


@dataclass
class SourceCapabilities:
    delta: bool = False  # native change feed (Graph delta, iManage events)
    webhooks: bool = False  # push notifications
    acl: bool = False  # per-object ACLs readable
    versions: bool = False  # source has native version history
    stable_ids: bool = True  # ids survive rename/move
    # Whether an empty result genuinely means "the source is empty" rather than
    # "the source was unreachable". A mounted directory can prove emptiness by listing
    # it; an API cannot — a revoked scope, an expired licence or a swallowed error all
    # look like zero objects. The engine refuses to tombstone a whole source on an empty
    # scan unless the connector claims this.
    verifiable_emptiness: bool = False


@dataclass
class SourceObjectObservation:
    """What a connector reports about one object during a scan."""

    external_id: str
    path: str
    name: str
    is_container: bool = False
    mime_type: str | None = None
    size_bytes: int | None = None
    mtime: datetime | None = None
    author_hint: str | None = None
    source_version_label: str | None = None
    change_hint: str | None = None  # etag/hash-like value; engine still verifies by content hash
    acl: list[dict] | None = None  # [AccessGrant as dict]; None = not readable
    # Where the connector already put this object's bytes, if it staged them during the
    # scan. Lets the fetch stage open a local file in another process instead of asking
    # the connector to locate the object again — which for an API source means
    # re-crawling the whole estate.
    staged_path: str | None = None


@dataclass
class ChangeBatch:
    observations: list[SourceObjectObservation] = field(default_factory=list)
    deleted_external_ids: list[str] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class UnsupportedOperation(Exception):
    """Raised by connectors for capabilities they don't have; engine falls back."""


@runtime_checkable
class SyncSource(Protocol):
    kind: str
    capabilities: SourceCapabilities

    def full_scan(self) -> Iterator[SourceObjectObservation]:
        """Enumerate everything, stable order. The engine checkpoints periodically."""
        ...

    def changes(self, cursor: str | None) -> ChangeBatch:
        """Incremental changes since cursor. Raise UnsupportedOperation if the
        source has no native delta; the engine falls back to full_scan diffing."""
        ...

    def fetch(self, external_id: str) -> BinaryIO:
        """Stream content for one object. Safe to call repeatedly."""
        ...
