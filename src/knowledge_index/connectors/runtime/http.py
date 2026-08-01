"""The HTTP client injected into every connector.

A thin async façade over ``httpx`` with two things the connectors assume: an SSRF
guard, and per-connection concurrency limiting.  Connectors call ``get``, ``post`` and
``stream`` and nothing else.

The SSRF guard matters here specifically because connector target URLs are partly
attacker-influenced: a OneDrive ``@odata.nextLink`` or a Notion file URL comes back
from an API response, and a firm-hosted SharePoint URL is typed in by an operator. A
connector must never be steerable into the appliance's own metadata service or the
Postgres/OpenSearch containers sitting next to it on the compose network.

The guard lives at the transport, which is the only place that sees every request. The
URL a connector hands over is not the only one fetched: ``follow_redirects`` is on and
redirects are resolved inside httpx, below this façade's methods, so a provider
answering ``302 Location: http://169.254.169.254/`` has to be caught there.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=15.0)
DEFAULT_MAX_CONNECTIONS = 10


class SsrfError(ValueError):
    """A request target resolved somewhere a connector must not reach."""


class _GuardedTransport(httpx.AsyncHTTPTransport):
    """Applies the SSRF guard to every connection, not just the one asked for.

    Every request leaves through a transport, redirect hops included, which makes this
    the one place a check cannot be routed around.
    """

    def __init__(self, check, **kwargs) -> None:
        super().__init__(**kwargs)
        self._check = check

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._check(str(request.url))
        return await super().handle_async_request(request)


# Loopback, link-local (cloud metadata lives at 169.254.169.254), and the RFC1918
# ranges the appliance's own service containers sit on.
def _is_blocked_address(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable now is not necessarily unresolvable at request time; let httpx
        # produce the real connection error rather than masking it as an SSRF verdict.
        return False
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_private
            or parsed.is_reserved
            or parsed.is_multicast
            or parsed.is_unspecified
        ):
            return True
    return False


class HttpClient:
    """Async HTTP client handed to connectors as ``self.http_client``.

    ``allow_private_hosts`` exists for on-prem sources that legitimately live on the
    firm's LAN (SharePoint 2019, an internal Confluence). It must be an explicit
    per-connection decision, never the default.

    ``proxy`` is for a firm that reaches the internet through a corporate proxy. It is
    an explicit argument rather than an ambient ``HTTPS_PROXY``: httpx would mount a
    separate transport for an environment proxy, and that transport would not carry the
    SSRF guard, so the guard would switch itself off wherever the variable happened to
    be set.
    """

    def __init__(
        self,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        allow_private_hosts: bool = False,
        verify: bool | str = True,
        proxy: str | None = None,
    ) -> None:
        self._allow_private_hosts = allow_private_hosts
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        )
        transport = _GuardedTransport(
            self._check, verify=verify, limits=limits, proxy=proxy
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            limits=limits,
            transport=transport,
            # ``mounts`` covers every scheme, so no request can be routed onto an
            # unguarded transport — including the ones httpx would otherwise build from
            # the environment's proxy variables.
            mounts={"all://": transport},
        )

    def _check(self, url: str) -> None:
        if self._allow_private_hosts:
            return
        parsed = urlparse(str(url))
        if parsed.scheme not in {"http", "https"}:
            raise SsrfError(f"refusing non-HTTP(S) connector URL: {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise SsrfError(f"connector URL has no host: {url!r}")
        if _is_blocked_address(host):
            raise SsrfError(
                f"refusing to fetch {host!r}: it resolves to a loopback, link-local or "
                "private address. Set allow_private_hosts on the connection if this "
                "source really is on the firm's own network."
            )

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        self._check(url)
        return await self._client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs):
        self._check(url)
        async with self._client.stream(method, url, **kwargs) as response:
            yield response

    @property
    def timeout(self):
        return self._client.timeout

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()
