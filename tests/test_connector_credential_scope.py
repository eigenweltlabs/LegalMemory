"""A connector's credential must only go to that connector's own hosts.

A download URL is not always the provider's. Notion lets a page editor add an
"external" file block whose address they typed themselves; Graph and Box hand back
pre-signed URLs on CDN hosts that want no credential at all. Attaching the bearer token
to whatever address came back means anyone who can edit a page in the connected
workspace collects the firm's integration token — and that token reads everything the
connector can reach, across every matter.
"""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import tempfile
import threading

import pytest

from knowledge_index.connectors.runtime.files import FileService, credential_allowed_for
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.runtime.tokens import StaticTokenProvider
from knowledge_index.connectors.sources.box import BOX_AUTH_HOSTS
from knowledge_index.connectors.sources.notion import NOTION_AUTH_HOSTS
from knowledge_index.connectors.sources.onedrive import GRAPH_AUTH_HOSTS


@pytest.mark.parametrize(
    ("url", "hosts", "allowed"),
    [
        ("https://api.notion.com/v1/blocks/1", NOTION_AUTH_HOSTS, True),
        ("https://files.notion.so/f/secret.pdf", NOTION_AUTH_HOSTS, True),
        # Notion's own file URLs are pre-signed S3 links; they need no credential.
        ("https://s3.us-west-2.amazonaws.com/secure.notion-static.com/x.pdf", NOTION_AUTH_HOSTS, False),
        # A look-alike must not match on substring.
        ("https://notion.so.evil.example/x.pdf", NOTION_AUTH_HOSTS, False),
        ("https://evil.example/notion.so/x.pdf", NOTION_AUTH_HOSTS, False),
        ("https://graph.microsoft.com/v1.0/me/drive", GRAPH_AUTH_HOSTS, True),
        # Graph's pre-authenticated download URLs carry their own token in the query.
        ("https://contoso-my.sharepoint.com/personal/x/_layouts/download.aspx", GRAPH_AUTH_HOSTS, False),
        ("https://api.box.com/2.0/files/1/content", BOX_AUTH_HOSTS, True),
        ("https://dl3.boxcloud.com/d/1/presigned", BOX_AUTH_HOSTS, False),
        ("", NOTION_AUTH_HOSTS, False),
        ("not-a-url", NOTION_AUTH_HOSTS, False),
    ],
)
def test_credential_scope(url, hosts, allowed):
    assert credential_allowed_for(url, hosts) is allowed


def test_no_hosts_declared_means_no_credential():
    """The default is to withhold: a forgotten declaration must fail visibly, not leak."""
    assert credential_allowed_for("https://api.notion.com/v1/x", ()) is False


class _Capture(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_GET(self):
        type(self).received.append(dict(self.headers))
        body = b"%PDF-1.4 content"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def capture_server():
    class Handler(_Capture):
        received: list[dict] = []

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], Handler
    server.shutdown()


class _Entity:
    """The shape ``download_from_url`` needs, standing in for a file entity."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.name = "invoice.pdf"
        self.entity_id = "blk-1"
        self.local_path = None


def _download(url: str, auth_hosts: tuple[str, ...]) -> None:
    files = FileService(tempfile.mkdtemp(), run_id="test")
    # allow_private_hosts only so the capture server can sit on loopback; in the real
    # attack the URL is a public host, which the SSRF guard permits by design.
    client = HttpClient(allow_private_hosts=True)

    async def drive():
        try:
            await files.download_from_url(
                entity=_Entity(url),
                client=client,
                auth=StaticTokenProvider("secret_ntn_FIRMS_TOKEN"),
                auth_hosts=auth_hosts,
            )
        finally:
            await client.aclose()

    asyncio.run(drive())


def test_a_page_editors_external_url_does_not_receive_the_token(capture_server):
    port, handler = capture_server
    _download(f"http://127.0.0.1:{port}/invoice.pdf", NOTION_AUTH_HOSTS)
    assert handler.received, "the download should still have been attempted"
    assert not any("Authorization" in headers for headers in handler.received), (
        "the firm's integration token was sent to an address a page editor chose"
    )


def test_the_providers_own_host_still_receives_the_token(capture_server):
    port, handler = capture_server
    # The capture server stands in for the provider: declaring its host as the
    # credential's home must restore the header.
    _download(f"http://127.0.0.1:{port}/invoice.pdf", ("127.0.0.1",))
    assert handler.received
    assert any(
        headers.get("Authorization") == "Bearer secret_ntn_FIRMS_TOKEN"
        for headers in handler.received
    ), "a legitimate download lost its credential"
