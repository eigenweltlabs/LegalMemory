"""A replay harness for driving connectors against recorded API responses.

Connectors are the part of this system we cannot exercise in CI against the real thing:
a live sync needs a firm's Microsoft, Google or Atlassian tenant. Without something in
between, the traversal code — pagination, licence fallbacks, delta tokens, permission
reads, per-item error handling — is only verified by having been read.

This harness closes that gap as far as it can be closed offline. Each test declares the
API responses a connector will see, keyed by request, and the connector runs for real
against them: its own request helpers, its own retry decorators, its own entity
construction, through the same bridge the sync engine uses. What is *not* verified is
whether the recorded payloads match what the provider actually sends today; that requires
a real tenant and is stated as such.

Matching is on ``METHOD path-substring``, most specific first, so a fixture set reads as
a list of endpoints rather than a pile of regexes. Some APIs put the interesting part of
the request somewhere other than the path — Drive asks for a folder's children with a
``q`` parameter and Dropbox with a JSON body, both against one URL — so a pattern may add
``| substring`` clauses that are matched against the query and body as well.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx

from knowledge_index.connectors.bridge import ConnectorAdapter
from knowledge_index.connectors.registry import get as get_connector
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.tokens import StaticTokenProvider


class UnexpectedRequest(AssertionError):
    """A connector asked for something the fixture set does not describe.

    Loud on purpose: a silent empty response would let a connector appear to work while
    skipping the call whose payload the test meant to exercise.
    """


class Recorded:
    """One canned response."""

    def __init__(
        self,
        payload: Any = None,
        *,
        status: int = 200,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        repeat: bool = True,
    ) -> None:
        self.payload = payload
        self.status = status
        self.content = content
        self.headers = headers or {}
        # Non-repeating entries let a test drive a sequence: first call throttled, second
        # succeeds, which is how the retry paths get covered.
        self.repeat = repeat
        self.calls = 0


class ReplayClient:
    """Stands in for ``HttpClient``, serving recorded responses.

    Deliberately mirrors the real client's surface (``get``/``post``/``stream`` plus
    ``aclose``) rather than being injected lower down, so connectors exercise their own
    header construction, parameter passing and status handling.
    """

    def __init__(self, routes: dict[str, Recorded | list[Recorded]]) -> None:
        self.routes: dict[str, list[Recorded]] = {
            key: (value if isinstance(value, list) else [value]) for key, value in routes.items()
        }
        self.requests: list[tuple[str, str]] = []
        # The same URL with a different query or body is a different question — a Drive
        # folder listing and a Dropbox path walk both put the interesting part there. The
        # full call is kept alongside the bare URL so assertions can reach it.
        self.calls: list[str] = []
        self.closed = False

    # -- matching -----------------------------------------------------------

    def _match(self, method: str, url: str, params: Any = None, json_body: Any = None) -> Recorded:
        self.requests.append((method.upper(), url))
        call = " ".join(
            part
            for part in (
                method.upper(),
                url,
                json.dumps(params, sort_keys=True, default=str) if params else "",
                json.dumps(json_body, sort_keys=True, default=str) if json_body else "",
            )
            if part
        )
        self.calls.append(call)

        candidates: list[tuple[int, str]] = []
        for key in self.routes:
            key_method, _, pattern = key.partition(" ")
            if key_method.upper() != method.upper():
                continue
            clauses = [clause for clause in pattern.split(" | ") if clause]
            if all(clause in call for clause in clauses):
                candidates.append((sum(len(clause) for clause in clauses), key))
        if not candidates:
            raise UnexpectedRequest(
                f"no recorded response for {call}\n"
                f"known routes:\n  " + "\n  ".join(sorted(self.routes))
            )
        # Longest pattern wins, so a specific endpoint beats a prefix.
        key = max(candidates)[1]
        entries = self.routes[key]
        for entry in entries:
            if entry.repeat or entry.calls == 0:
                entry.calls += 1
                return entry
        entries[-1].calls += 1
        return entries[-1]

    def _response(
        self, method: str, url: str, params: Any = None, json_body: Any = None
    ) -> httpx.Response:
        recorded = self._match(method, url, params, json_body)
        request = httpx.Request(method.upper(), url if url.startswith("http") else f"https://x/{url}")
        if recorded.content is not None:
            return httpx.Response(
                recorded.status, content=recorded.content, headers=recorded.headers, request=request
            )
        body = json.dumps(recorded.payload if recorded.payload is not None else {})
        headers = {"content-type": "application/json", **recorded.headers}
        return httpx.Response(recorded.status, content=body.encode(), headers=headers, request=request)

    # -- HttpClient surface -------------------------------------------------

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return self._response(method, url, kwargs.get("params"), kwargs.get("json"))

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
        response = self._response(method, url, kwargs.get("params"), kwargs.get("json"))
        # aiter_bytes on a non-streamed Response works because the body is already read.
        yield response

    @property
    def is_closed(self) -> bool:
        return self.closed

    async def aclose(self) -> None:
        self.closed = True

    # -- assertions ---------------------------------------------------------

    def called(self, needle: str) -> bool:
        return any(needle in call for call in self.calls)

    def call_count(self, needle: str) -> int:
        return sum(1 for call in self.calls if needle in call)


def build(
    short_name: str,
    routes: dict[str, Recorded | list[Recorded]],
    *,
    staging: Any,
    config: dict | None = None,
    token: str = "test-token",
    cursor_data: dict | None = None,
    node_selections: list | None = None,
) -> tuple[ConnectorAdapter, ReplayClient]:
    """Build a real connector wired to a replay client, ready to drive.

    Uses the registry so the test exercises the same construction path the pipeline does,
    including each connector's typed config and cursor class.
    """
    from knowledge_index.connectors.bridge import LoopRunner
    from knowledge_index.connectors.registry import _build_config, _build_cursor

    spec = get_connector(short_name)
    source_class = spec.load()
    client = ReplayClient(routes)
    logger = ContextualLogger(source=short_name, run_id="replay")
    files = FileService(staging, run_id=short_name)

    # Same order as the registry: the source is created on the loop it will be driven on.
    runner = LoopRunner()
    try:
        source = runner.run(
            source_class.create(
                auth=StaticTokenProvider(token),
                logger=logger,
                http_client=client,
                config=_build_config(source_class, {**spec.config_defaults, **(config or {})}),
            )
        )
    except BaseException:
        runner.close()
        raise

    adapter = ConnectorAdapter(
        short_name,
        source,
        file_service=files,
        cursor=_build_cursor(source_class, cursor_data),
        node_selections=node_selections,
        runner=runner,
        http_client=client,
        logger=logger,
    )
    return adapter, client
