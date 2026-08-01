"""The connector HTTP client must not be steerable into the appliance's own network.

Connector target URLs are only partly ours. A ``@odata.nextLink``, a Box download link
and a Notion file URL all come back from an API response, and an on-prem SharePoint URL
is typed in by an operator. The appliance runs beside Postgres and OpenSearch on a
compose network, and on a cloud host beside a metadata service at 169.254.169.254, so a
connector that can be pointed at an arbitrary address is a way to read all of it.

The redirect case is the one worth pinning: redirects are resolved inside httpx, below
the client's own methods, so the address finally connected to is not necessarily the one
a connector passed in.
"""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import threading

import pytest

from knowledge_index.connectors.runtime.http import _GuardedTransport, HttpClient, SsrfError


def run(coroutine):
    return asyncio.run(coroutine)


def _serve(handler) -> tuple[int, socketserver.TCPServer]:
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], server


@pytest.fixture()
def internal_service():
    """Stands in for the metadata service or a sidecar container."""

    class Internal(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"db_password=hunter2")

        def log_message(self, *args):
            pass

    port, server = _serve(Internal)
    yield port
    server.shutdown()


@pytest.fixture()
def redirector(internal_service):
    """A reachable host that answers with a redirect into the internal service."""

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{internal_service}/")
            self.end_headers()

        def log_message(self, *args):
            pass

    port, server = _serve(Redirect)
    yield port
    server.shutdown()


def _allow_only(port: int):
    """Let one origin through, stand-in for a legitimate public provider host."""
    real_check = HttpClient._check

    def check(self, url):
        if f":{port}" in str(url):
            return
        return real_check(self, url)

    return check


def test_direct_fetch_of_a_private_address_is_refused(internal_service):
    client = HttpClient()
    try:
        with pytest.raises(SsrfError):
            run(client.get(f"http://127.0.0.1:{internal_service}/"))
    finally:
        run(client.aclose())


def test_a_redirect_cannot_reach_a_private_address(monkeypatch, redirector):
    """The first hop is allowed; where it points must still be checked."""
    monkeypatch.setattr(HttpClient, "_check", _allow_only(redirector))
    client = HttpClient()
    try:
        with pytest.raises(SsrfError):
            run(client.get(f"http://127.0.0.1:{redirector}/"))
    finally:
        run(client.aclose())


def test_streamed_downloads_are_guarded_on_redirect(monkeypatch, redirector):
    """File content is fetched with ``stream``; it takes the same path."""
    monkeypatch.setattr(HttpClient, "_check", _allow_only(redirector))
    client = HttpClient()

    async def drive():
        async with client.stream("GET", f"http://127.0.0.1:{redirector}/"):
            pass

    try:
        with pytest.raises(SsrfError):
            run(drive())
    finally:
        run(client.aclose())


def test_non_http_schemes_are_refused():
    client = HttpClient()
    try:
        with pytest.raises(SsrfError):
            run(client.get("file:///etc/passwd"))
    finally:
        run(client.aclose())


def test_on_prem_sources_can_opt_in(internal_service):
    """A firm's own SharePoint really does live on a private address."""
    client = HttpClient(allow_private_hosts=True)
    try:
        response = run(client.get(f"http://127.0.0.1:{internal_service}/"))
        assert response.status_code == 200
    finally:
        run(client.aclose())


def test_no_request_can_reach_an_unguarded_transport(monkeypatch):
    """Every route httpx might pick has to carry the guard.

    httpx mounts a separate transport per environment proxy, and a redirect hop routed
    onto one of those would never be checked — an ``HTTPS_PROXY`` in the appliance's
    environment would quietly switch the guard off for exactly the hop that matters.
    Asserted structurally because the alternative is dialling a real proxy.
    """
    monkeypatch.setenv("HTTP_PROXY", "http://198.51.100.9:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://198.51.100.9:3128")
    client = HttpClient()
    try:
        transports = [client._client._transport, *client._client._mounts.values()]
        assert transports, "expected at least the default transport"
        assert all(
            isinstance(transport, _GuardedTransport)
            for transport in transports
            if transport is not None
        ), f"an unguarded transport is reachable: {transports}"
    finally:
        run(client.aclose())
