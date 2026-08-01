"""Typed connector exceptions.

The distinctions are load-bearing because connector control flow depends on them: a
connector catches ``SourceAuthError`` to abort a
sync, ``SourceEntityNotFoundError`` / ``SourceEntityForbiddenError`` to skip one
object, and lets ``SourceRateLimitError`` / ``SourceServerError`` reach the retry
decorator.  Collapsing these into one class would silently turn "skip this file"
into "abandon this firm's index", so the distinctions are load-bearing.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class for every connector-raised failure."""


class SourceError(ConnectorError):
    """A source-level failure. Aborts the sync unless a caller narrows it."""

    def __init__(self, message: str, *, source_short_name: str = "", **extra: object) -> None:
        super().__init__(message)
        self.source_short_name = source_short_name
        self.extra = extra


class SourceAuthError(SourceError):
    """Credentials are dead and cannot be refreshed — abort, do not tombstone.

    Critical: the sync engine must NOT interpret this as "the source is empty".
    A revoked token would otherwise tombstone the firm's entire corpus.
    """

    def __init__(
        self,
        message: str,
        *,
        source_short_name: str = "",
        token_provider_kind: object = None,
        **extra: object,
    ) -> None:
        super().__init__(message, source_short_name=source_short_name, **extra)
        self.token_provider_kind = token_provider_kind


class SourceTokenRefreshError(SourceAuthError):
    """A refresh attempt failed."""


class SourceValidationError(SourceError):
    """Credentials or configuration are unusable; raised by ``validate()``."""


class SourceRateLimitError(SourceError):
    """Upstream asked us to slow down. Retryable; carries ``retry_after`` seconds."""

    def __init__(
        self, message: str, *, retry_after: float = 1.0, source_short_name: str = "", **extra: object
    ) -> None:
        super().__init__(message, source_short_name=source_short_name, **extra)
        self.retry_after = retry_after


class SourceServerError(SourceError):
    """Upstream 5xx. Retryable with backoff."""


class SourceEntityError(SourceError):
    """A single object failed. Skippable — the rest of the scan continues."""

    def __init__(
        self, message: str, *, entity_id: str = "", source_short_name: str = "", **extra: object
    ) -> None:
        super().__init__(message, source_short_name=source_short_name, **extra)
        self.entity_id = entity_id


class SourceEntityForbiddenError(SourceEntityError):
    """403 on one object — the connector decides whether that is fatal."""


class SourceEntityNotFoundError(SourceEntityError):
    """404 on one object; usually deleted between enumeration and fetch."""


class SourceEntitySkippedError(SourceEntityError):
    """The connector deliberately skipped one object."""


class SourceFileDownloadError(SourceEntityError):
    """Content could not be downloaded."""


class FileSkippedException(ConnectorError):
    """Content was skipped by policy (extension not allowed, too large).

    Most connectors catch this by name to continue past one unusable object, so it
    stays a distinct type.
    """

    def __init__(self, reason: str, filename: str = "") -> None:
        super().__init__(f"skipped {filename or 'file'}: {reason}")
        self.reason = reason
        self.filename = filename


class EntityProcessingError(ConnectorError):
    """A connector could not turn an API payload into an entity."""


class TokenRefreshNotSupportedError(SourceAuthError):
    """``force_refresh()`` was called on a provider that cannot refresh."""
