"""Short-lived capability links for exporting authorized original documents.

The MCP server cannot write into a caller's workspace: in the normal deployment it
runs in a container while the MCP client owns the local filesystem.  A download
capability bridges that boundary without putting the document bytes in the model
context.  Capabilities are deliberately process-local, unguessable, and short-lived.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadCapability:
    token: str
    document_id: str
    version_id: str
    source_object_id: str
    content_hash: str
    filename: str
    mime_type: str
    size_bytes: int
    principals: tuple[str, ...]
    expires_at_monotonic: float


class DownloadTokenStore:
    """Issue and resolve time-limited bearer capabilities for original blobs."""

    def __init__(self, *, ttl_seconds: int = 300) -> None:
        if ttl_seconds <= 0:
            raise ValueError("download capability TTL must be positive")
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, DownloadCapability] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        document_id: str,
        version_id: str,
        source_object_id: str,
        content_hash: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        principals: set[str],
    ) -> DownloadCapability:
        now = time.monotonic()
        entry = DownloadCapability(
            token=secrets.token_urlsafe(32),
            document_id=document_id,
            version_id=version_id,
            source_object_id=source_object_id,
            content_hash=content_hash,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            principals=tuple(sorted(principals)),
            expires_at_monotonic=now + self.ttl_seconds,
        )
        with self._lock:
            self._remove_expired(now)
            self._entries[entry.token] = entry
        return entry

    def resolve(self, token: str) -> DownloadCapability | None:
        now = time.monotonic()
        with self._lock:
            self._remove_expired(now)
            return self._entries.get(token)

    def revoke(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)

    def _remove_expired(self, now: float) -> None:
        expired = [
            token
            for token, entry in self._entries.items()
            if entry.expires_at_monotonic <= now
        ]
        for token in expired:
            self._entries.pop(token, None)
