"""Adapt a source connector onto the ``SyncSource`` contract.

Connectors are async generators of richly typed entities. The sync engine is
synchronous and wants flat
:class:`~knowledge_index.sync.base.SourceObjectObservation` records. This module is the
only place that gap is bridged, so connectors stay free of engine concerns and the
engine stays free of connector ones.

What happens here and nowhere else:

1. **Entity flattening** — id, name and timestamps are resolved from the schema's field
   markers rather than guessed from attribute names.
2. **Content staging** — connectors write content while enumerating; the staged path is
   recorded on the observation so ``fetch()`` is a local file open in any process.
3. **Deletion routing** — delta feeds report removals as entities. They become
   ``deleted_external_ids`` so an incremental sync can tombstone; without this a
   document deleted at source stays searchable.
4. **ACL translation** — viewers become source-object grants. The security-critical
   step: wrong here either hides a firm's corpus or leaks across an ethical wall.

A connector that reads no ACLs yields ``acl=None``, which the permission compiler
treats as unknown and therefore fail-closed.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from knowledge_index.connectors.entities.flags import (
    entity_external_id,
    entity_mtime,
    entity_name,
    entity_version_token,
    flagged_value,
    is_deletion,
)
from knowledge_index.connectors.entities.text import is_container, render_text
from knowledge_index.connectors.runtime.errors import FileSkippedException
from knowledge_index.sync.base import (
    ChangeBatch,
    SourceCapabilities,
    SourceObjectObservation,
    UnsupportedOperation,
)

__all__ = [
    "ConnectorAdapter",
    "LoopRunner",
    "entity_external_id",
    "entity_mtime",
    "entity_name",
    "entity_path",
    "flagged_value",
    "is_container",
    "is_content_entity",
    "render_text",
    "translate_access",
]


def entity_path(entity: Any, name: str) -> str:
    """Reconstruct a human-meaningful path from the entity's breadcrumb ancestry."""
    parts = [
        str(crumb.name).strip()
        for crumb in (getattr(entity, "breadcrumbs", None) or [])
        if getattr(crumb, "name", None) and str(crumb.name).strip()
    ]
    return "/".join([*parts, name]) if parts else name


def translate_access(access: Any) -> list[dict] | None:
    """Turn an entity's access metadata into source-object grants.

    ``None`` means the connector cannot read permissions — distinct from an empty viewer
    list, which means "only explicitly granted principals". Both are fail-closed, but
    only the first is a capability gap rather than a real restriction, and the engine
    needs to be able to tell them apart.

    Principals stay in the source's own namespace here. Mapping them onto this
    appliance's identities is a separate, explicit step — see
    :mod:`knowledge_index.connectors.principals`.
    """
    if access is None:
        return None
    grants: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(raw: object, effect: str) -> None:
        principal = str(raw).strip().casefold()
        if not principal or (principal, effect) in seen:
            return
        seen.add((principal, effect))
        grants.append(
            {
                "principal": principal,
                "principal_kind": principal.partition(":")[0] or "group",
                "effect": effect,
                "origin": "connector",
            }
        )

    # Denies first for readability; the compiler applies deny-wins regardless of order.
    for viewer in getattr(access, "denied_viewers", None) or []:
        add(viewer, "deny")
    for viewer in getattr(access, "viewers", None) or []:
        add(viewer, "allow")
    if getattr(access, "is_public", False):
        # "Public" in these sources means every authenticated member of the tenant,
        # never anonymous access.
        add("role:authenticated", "allow")
    return sorted(grants, key=lambda grant: (grant["effect"], grant["principal"]))


def is_content_entity(entity: Any) -> bool:
    """Whether an entity carries indexable content rather than being a container.

    Containers (drives, sites, channels, notebooks) are yielded to give descendants
    breadcrumb context. They are not documents, and indexing them fills the corpus with
    folder stubs that match every query weakly.

    Text-only documents are recognised by rendering their embeddable fields, not by
    checking ``textual_representation`` — connectors do not fill that. Relying on it
    dropped every message and page silently, so a Teams sync reported success and indexed
    nothing.
    """
    if getattr(entity, "local_path", None):
        return True
    # A schema that declares `local_path` is a file: its content is the bytes, not its
    # metadata. If the download did not happen, indexing it anyway would add a stub
    # containing only a filename — an empty document that matches weakly and buries real
    # results. Skip it and let the next sync retry.
    if "local_path" in type(entity).model_fields:
        return False
    if is_container(entity):
        return False
    return bool(render_text(entity))


class LoopRunner:
    """Runs connector coroutines on one private event loop.

    The caller may already be inside a running loop (the web app) or not (the CLI and
    the worker). A dedicated loop on its own thread behaves identically in both cases,
    and keeps a multi-hour crawl from blocking the caller's loop. One per connector, not
    one per operation — a loop and thread per document would exhaust both.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ki-connector-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


class ConnectorAdapter:
    """A source connector presented as a Knowledge Index ``SyncSource``."""

    def __init__(
        self,
        kind: str,
        source: Any,
        *,
        file_service: Any,
        cursor: Any = None,
        node_selections: list | None = None,
        runner: LoopRunner | None = None,
        owns_runner: bool = True,
        http_client: Any = None,
        capabilities: SourceCapabilities | None = None,
        logger: Any = None,
    ) -> None:
        self.kind = kind
        self._source = source
        self._files = file_service
        self._cursor = cursor
        # Which subtrees to sync. Empty means the whole source — kept as the default so an
        # unscoped connection behaves as before, while the admin UI pushes operators to
        # choose. See docs/connector-scoping.md.
        self._node_selections = list(node_selections or [])
        self._logger = logger
        self._http_client = http_client
        self._runner = runner or LoopRunner()
        # The adapter shuts the loop down by default, including one handed to it, because
        # it is the object with a lifecycle the caller already manages. Pass
        # ``owns_runner=False`` only when a caller drives several adapters on one loop.
        self._owns_runner = owns_runner
        source_class = type(source)
        self.capabilities = capabilities or SourceCapabilities(
            delta=bool(getattr(source_class, "supports_continuous", False)),
            webhooks=False,
            acl=bool(getattr(source_class, "supports_access_control", False)),
            versions=False,
            stable_ids=True,
        )
        # Membership freshness at the policy interval, for sources that can afford it.
        self.cheap_memberships = bool(getattr(source_class, "cheap_memberships", False))

    # -- SyncSource ---------------------------------------------------------

    def full_scan(self) -> Iterator[SourceObjectObservation]:
        # A full scan must ignore an old provider token, but it must still give the
        # connector a writable cursor in which to capture the new Drive Changes token,
        # Graph deltaLink, Gmail history id, etc. Passing ``None`` here made every full
        # scan throw that checkpoint away, so the next scheduled run was another crawl.
        if self._cursor is not None:
            from knowledge_index.connectors.cursors.state import SyncCursor

            self._cursor = SyncCursor(
                self._cursor.sync_id,
                self._cursor.cursor_schema,
            )
        for observation, _deleted in self._drain(cursor=self._cursor):
            if observation is not None:
                yield observation

    def changes(self, cursor: str | None) -> ChangeBatch:
        if not self.capabilities.delta:
            raise UnsupportedOperation(
                f"{self.kind} has no native change feed; the engine diffs a full scan"
            )
        if self._cursor is None:
            raise UnsupportedOperation(f"{self.kind} was built without a cursor")
        observations: list[SourceObjectObservation] = []
        deleted: list[str] = []
        for observation, deleted_id in self._drain(cursor=self._cursor):
            if observation is not None:
                observations.append(observation)
            if deleted_id:
                deleted.append(deleted_id)
        return ChangeBatch(
            observations=observations,
            deleted_external_ids=deleted,
            next_cursor=self.cursor_state(),
            # Connectors paginate internally and return once the feed is drained, so one
            # batch per call is the whole delta.
            has_more=False,
        )

    def fetch(self, external_id: str) -> BinaryIO:
        """Open the content staged for this object.

        Deliberately does not re-enumerate on a miss. Re-crawling a SaaS estate to
        recover one file is quadratic in corpus size and throttles the firm's tenant;
        a miss means the sync stage has to run first, and saying so is the right
        failure.
        """
        raise UnsupportedOperation(
            f"{self.kind} content is staged during sync — fetch by staged path, not by id"
        )

    def open_staged(self, path: str | None, external_id: str = "") -> BinaryIO:
        """Open a path recorded on the observation during the scan."""
        if not path:
            raise FileNotFoundError(
                f"no staged content for {external_id or 'object'}; re-run the sync for "
                f"source kind {self.kind!r} before fetching"
            )
        return Path(path).open("rb")

    @property
    def node_selections(self) -> list:
        """The subtrees this connector was scoped to (empty = the whole source)."""
        return list(self._node_selections)

    async def browse(self, parent_node_id: str | None = None) -> list:
        """List the children of one node, for picking sync roots in the admin UI."""
        return await self._source.get_browse_children(parent_node_id)

    def browse_children(self, parent_node_id: str | None = None) -> list[dict]:
        """Synchronous ``browse`` for the admin API."""
        nodes = self._runner.run(self.browse(parent_node_id))
        return [
            node.model_dump() if hasattr(node, "model_dump") else dict(node) for node in nodes
        ]

    def memberships(self) -> list[dict]:
        """Mirror the source's group memberships, if it can report them.

        Without this, a grant to ``group:entra:<guid>`` can never match a caller: the
        appliance has no way to know who belongs to that group. Group-shared documents
        are the normal case in a firm, so this is what makes mirrored ACLs usable rather
        than merely present.
        """
        generator = getattr(self._source, "generate_access_control_memberships", None)
        if generator is None or not self.capabilities.acl:
            return []
        tuples: list[dict] = []
        agen = generator()
        while True:
            try:
                membership = self._runner.run(_anext(agen))
            except StopAsyncIteration:
                break
            except NotImplementedError:
                return []
            tuples.append(
                {
                    "member_id": str(membership.member_id).casefold(),
                    "member_type": str(membership.member_type).casefold(),
                    "group_id": str(membership.group_id).casefold(),
                    "group_name": getattr(membership, "group_name", None),
                }
            )
        return tuples

    def cursor_state(self) -> str | None:
        """The cursor to persist, or ``None`` if this connector keeps none.

        An empty-but-present cursor serializes to ``"{}"`` rather than ``None`` so a
        delta run that had nothing to remember is distinguishable from one that never
        happened — otherwise the next sync silently downgrades to a full scan.
        """
        if self._cursor is None:
            return None
        try:
            return json.dumps(self._cursor.data or {}, sort_keys=True)
        except (AttributeError, TypeError):
            return None

    def close(self) -> None:
        if self._http_client is not None:
            try:
                self._runner.run(self._http_client.aclose())
            except Exception:  # noqa: BLE001 - teardown must not mask a sync failure
                pass
        if self._owns_runner:
            self._runner.close()

    def __enter__(self) -> ConnectorAdapter:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- internals ----------------------------------------------------------

    def _drain(self, *, cursor: Any) -> Iterator[tuple[SourceObjectObservation | None, str | None]]:
        """Pull the async generator one entity at a time through the private loop.

        Draining incrementally keeps memory flat across a multi-million-document estate
        and lets the engine checkpoint as it goes.
        """
        agen = self._source.generate_entities(
            cursor=cursor,
            files=self._files,
            node_selections=self._node_selections or None,
        )
        while True:
            try:
                entity = self._runner.run(_anext(agen))
            except StopAsyncIteration:
                return
            except FileSkippedException as exc:
                # One unconvertible object must never abort a firm's whole sync.
                if self._logger:
                    self._logger.debug(f"skipped {exc.filename}: {exc.reason}")
                continue
            if is_deletion(entity):
                external_id = entity_external_id(entity)
                yield None, external_id
                continue
            yield self._observation(entity), None

    def _observation(self, entity: Any) -> SourceObjectObservation | None:
        external_id = entity_external_id(entity)
        if not external_id or not is_content_entity(entity):
            return None
        name = entity_name(entity) or external_id
        version = entity_version_token(entity)
        local_path = getattr(entity, "local_path", None)
        if local_path:
            size = getattr(entity, "size", None) or _file_size(local_path)
        else:
            text = render_text(entity)
            local_path = str(self._files.stage_text(external_id, text, version=version))
            size = len(text.encode("utf-8"))

        return SourceObjectObservation(
            external_id=external_id,
            path=entity_path(entity, name),
            name=name,
            mime_type=getattr(entity, "mime_type", None) or _fallback_mime(entity),
            size_bytes=size,
            mtime=entity_mtime(entity),
            author_hint=_author_hint(entity),
            source_version_label=version,
            change_hint=version,
            acl=translate_access(getattr(entity, "access", None)),
            staged_path=str(local_path),
        )


async def _anext(agen):
    return await agen.__anext__()


def _file_size(path: str) -> int | None:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def _fallback_mime(entity: Any) -> str:
    return "application/octet-stream" if getattr(entity, "local_path", None) else "text/plain"


def _author_hint(entity: Any) -> str | None:
    for attribute in ("author", "created_by", "sender", "from_", "owner"):
        value = getattr(entity, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:255]
        if isinstance(value, dict):
            for key in ("email", "emailAddress", "displayName", "name"):
                nested = value.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()[:255]
    return None
