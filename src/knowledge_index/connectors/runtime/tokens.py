"""Auth providers handed to a connector at construction time.

Connectors touch exactly four members of this contract —
``get_token()``, ``force_refresh()``, ``supports_refresh`` and ``provider_kind`` —
so that is the whole surface we have to own.  Three implementations cover every
connector we ship:

``OAuthTokenProvider``      OAuth2 with proactive refresh, credentials persisted encrypted
``StaticTokenProvider``     a bare token (PAT, app password, ``validate()`` probes)
``DirectCredentialProvider``structured non-token credentials (SharePoint app auth)

Refresh is serialized under a lock and the refreshed pair is persisted through a
caller-supplied sink, so a rotating-refresh provider (Confluence, Dropbox) cannot
lose the new refresh token when two workers race.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Generic, Protocol, TypeVar, runtime_checkable

from knowledge_index.connectors.runtime.errors import (
    SourceAuthError,
    TokenRefreshNotSupportedError,
)

# Refresh a little before the reported expiry so an in-flight request cannot land on a
# token that expires mid-call.
REFRESH_LIFETIME_FRACTION = 0.80
MIN_REFRESH_INTERVAL_SECONDS = 60
MAX_REFRESH_INTERVAL_SECONDS = 50 * 60
DEFAULT_REFRESH_INTERVAL_SECONDS = 25 * 60

REFRESHABLE_OAUTH_TYPES = frozenset({"with_refresh", "with_rotating_refresh"})


class AuthProviderKind(str, Enum):
    """Discriminator carried into error messages so failures name their auth mode."""

    OAUTH = "oauth"
    STATIC = "static"
    AUTH_PROVIDER = "auth_provider"
    CREDENTIAL = "credential"


@runtime_checkable
class SourceAuthProvider(Protocol):
    """Base contract every connector receives as ``self.auth``."""

    @property
    def provider_kind(self) -> AuthProviderKind: ...

    @property
    def supports_refresh(self) -> bool: ...


@runtime_checkable
class TokenProviderProtocol(SourceAuthProvider, Protocol):
    """Token-based auth — what 90% of connectors declare."""

    async def get_token(self) -> str: ...

    async def force_refresh(self) -> str: ...


class StaticTokenProvider:
    """A fixed token. Cannot refresh; a 401 is terminal and says so."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("StaticTokenProvider requires a non-empty token")
        self._token = token

    @property
    def provider_kind(self) -> AuthProviderKind:
        return AuthProviderKind.STATIC

    @property
    def supports_refresh(self) -> bool:
        return False

    async def get_token(self) -> str:
        return self._token

    async def force_refresh(self) -> str:
        raise TokenRefreshNotSupportedError(
            "this connection was configured with a static token; it cannot be refreshed. "
            "Re-authorize the connector to obtain new credentials."
        )


CredentialsT = TypeVar("CredentialsT")


class DirectCredentialProvider(Generic[CredentialsT]):
    """Structured credentials (client id/secret, certificate thumbprint, …).

    Used where the connector performs its own token acquisition — SharePoint Online
    app-only auth mints per-resource tokens itself.
    """

    def __init__(self, credentials: CredentialsT) -> None:
        self._credentials = credentials

    @property
    def credentials(self) -> CredentialsT:
        return self._credentials

    @property
    def provider_kind(self) -> AuthProviderKind:
        return AuthProviderKind.CREDENTIAL

    @property
    def supports_refresh(self) -> bool:
        return False


# (access_token, refresh_token, expires_in_seconds)
RefreshResult = tuple[str, str | None, int | None]
RefreshFn = Callable[[str], Awaitable[RefreshResult]]
PersistFn = Callable[[dict], Awaitable[None]]


class OAuthTokenProvider:
    """OAuth2 access token with proactive, serialized refresh.

    ``refresh`` performs the network call (see :mod:`knowledge_index.connectors.runtime.oauth`)
    and ``persist`` durably stores the resulting credential dict.  Both are injected so
    this class stays free of database and HTTP concerns and is directly unit-testable.
    """

    def __init__(
        self,
        credentials: dict,
        *,
        oauth_type: str | None,
        refresh: RefreshFn | None = None,
        persist: PersistFn | None = None,
        source_short_name: str = "",
    ) -> None:
        token = str(credentials.get("access_token") or "")
        refresh_token = credentials.get("refresh_token")
        if not token and not refresh_token:
            raise ValueError(
                f"no access_token or refresh_token in credentials for {source_short_name!r}"
            )
        self._token = token
        self._refresh_token = str(refresh_token) if refresh_token else None
        self._oauth_type = oauth_type
        self._refresh = refresh
        self._persist = persist
        self._source_short_name = source_short_name
        self._can_refresh = (
            oauth_type in REFRESHABLE_OAUTH_TYPES
            and self._refresh_token is not None
            and refresh is not None
        )
        # Force one refresh up front: a token loaded from the database has an unknown
        # remaining lifetime, and starting a multi-hour crawl on a token with two
        # minutes left fails deep into the scan instead of immediately.
        self._needs_initial_refresh = self._can_refresh
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def provider_kind(self) -> AuthProviderKind:
        return AuthProviderKind.OAUTH

    @property
    def supports_refresh(self) -> bool:
        return self._can_refresh

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    async def get_token(self) -> str:
        if not self._can_refresh:
            if not self._token:
                raise SourceAuthError(
                    "connection has no usable access token and cannot refresh; re-authorize it",
                    source_short_name=self._source_short_name,
                    token_provider_kind=AuthProviderKind.OAUTH,
                )
            return self._token
        if not self._needs_initial_refresh and time.monotonic() < self._expires_at:
            return self._token
        return await self._refresh_once()

    async def force_refresh(self) -> str:
        if not self._can_refresh:
            raise TokenRefreshNotSupportedError(
                f"{self._source_short_name or 'connection'} cannot refresh: no refresh token "
                "was issued. Re-authorize the connector.",
                source_short_name=self._source_short_name,
                token_provider_kind=AuthProviderKind.OAUTH,
            )
        # Expire the cache so a concurrent holder of the lock cannot serve the token we
        # just learned was rejected.
        self._expires_at = 0.0
        self._needs_initial_refresh = True
        return await self._refresh_once()

    async def _refresh_once(self) -> str:
        async with self._lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if not self._needs_initial_refresh and time.monotonic() < self._expires_at:
                return self._token
            assert self._refresh is not None and self._refresh_token is not None
            access_token, refresh_token, expires_in = await self._refresh(self._refresh_token)
            self._token = access_token
            # Rotating-refresh providers invalidate the old refresh token on use, so a
            # dropped rotation here would permanently break the connection.
            if refresh_token:
                self._refresh_token = refresh_token
            self._expires_at = time.monotonic() + _refresh_interval(expires_in)
            self._needs_initial_refresh = False
            if self._persist is not None:
                await self._persist(
                    {
                        "access_token": self._token,
                        "refresh_token": self._refresh_token,
                        "expires_in": expires_in,
                    }
                )
            return self._token


def _refresh_interval(expires_in: int | None) -> float:
    if not expires_in or expires_in <= 0:
        return float(DEFAULT_REFRESH_INTERVAL_SECONDS)
    interval = float(expires_in) * REFRESH_LIFETIME_FRACTION
    return max(MIN_REFRESH_INTERVAL_SECONDS, min(interval, MAX_REFRESH_INTERVAL_SECONDS))
