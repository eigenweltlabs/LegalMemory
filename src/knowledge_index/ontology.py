"""Pluggable ontology system — one system for every controlled vocabulary.

A deployment plugs in one ontology *artifact* (built by
``scripts/build_ontology_artifact.py``; LMSS is the shipped default) and
activates *facets* of it (v1: ``doc_type``). The scoped view — artifact minus
disabled nodes, restricted to active facets — is the sole vocabulary source for
the extraction agent, retrieval filters, MCP tools, and the dashboard.

Design rules:
- Navigation is deterministic: ``roots``/``children``/``node`` are pure lookups.
  ``search`` (lexical, stable ranking) exists for retrieval-time filter
  discovery, never for extraction-time classification.
- Any node, interior or leaf, is a valid classification. "Stopping high" is the
  honest catch-all; depth pressure is the health signal.
- Every scoped view has a stable fingerprint recorded per document, so a scope
  or artifact change knows exactly which documents were typed under it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Iterable

PACKAGED_DATA = "ontology_data"


@dataclass(frozen=True)
class OntologyNode:
    id: str
    label: str
    definition: str = ""
    synonyms: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class OntologyArtifact:
    """A parsed, immutable ontology artifact (all facets, unscoped)."""

    name: str
    version: str
    source_sha256: str
    facets: dict[str, tuple[str, ...]]
    nodes: dict[str, OntologyNode]
    children: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: bytes) -> "OntologyArtifact":
        payload = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        nodes: dict[str, OntologyNode] = {}
        for node_id, entry in payload["nodes"].items():
            nodes[node_id] = OntologyNode(
                id=node_id,
                label=entry["l"],
                definition=entry.get("d", ""),
                synonyms=tuple(entry.get("s", ())),
                parents=tuple(entry.get("p", ())),
            )
        children: dict[str, list[str]] = {}
        for node in nodes.values():
            for parent in node.parents:
                children.setdefault(parent, []).append(node.id)
        return cls(
            name=payload["name"],
            version=payload["version"],
            source_sha256=payload["source_sha256"],
            facets={facet: tuple(roots) for facet, roots in payload["facets"].items()},
            nodes=nodes,
            children={
                parent: tuple(sorted(ids, key=lambda i: nodes[i].label))
                for parent, ids in children.items()
            },
        )


class OntologyScope:
    """The active view: one artifact, active facets, minus disabled subtrees."""

    def __init__(
        self,
        artifact: OntologyArtifact,
        active_facets: tuple[str, ...],
        disabled_nodes: frozenset[str],
    ) -> None:
        self.artifact = artifact
        self.active_facets = tuple(
            facet for facet in active_facets if facet in artifact.facets
        )
        self.disabled_nodes = disabled_nodes

        # Visibility = reachable from an active facet root without crossing a
        # disabled node. Computed eagerly; facet subtrees are small relative to
        # the artifact (doc_type in LMSS: ~1.5k of 18k nodes).
        self._facet_of: dict[str, str] = {}
        visible: set[str] = set()
        self._root_ids: list[str] = []
        for facet in self.active_facets:
            for root in artifact.facets[facet]:
                if root in disabled_nodes or root not in artifact.nodes:
                    continue
                self._root_ids.append(root)
                stack = [root]
                while stack:
                    current = stack.pop()
                    if current in visible:
                        continue
                    visible.add(current)
                    self._facet_of.setdefault(current, facet)
                    for child in artifact.children.get(current, ()):  # noqa: E501
                        if child not in disabled_nodes:
                            stack.append(child)
        self.visible: frozenset[str] = frozenset(visible)
        self._ancestor_cache: dict[str, frozenset[str]] = {}

        digest = hashlib.sha256(
            "|".join(
                [
                    artifact.name,
                    artifact.version,
                    artifact.source_sha256,
                    ",".join(self.active_facets),
                    ",".join(sorted(disabled_nodes)),
                ]
            ).encode()
        ).hexdigest()
        self.fingerprint: str = digest[:16]

    # -- deterministic navigation (the extraction agent's entire world) -----

    def roots(self) -> list[dict]:
        return [self.describe(node_id) for node_id in self._root_ids]

    def children(self, node_id: str) -> list[dict]:
        if node_id not in self.visible:
            return []
        return [
            self.describe(child)
            for child in self.artifact.children.get(node_id, ())
            if child in self.visible
        ]

    def node(self, node_id: str) -> dict | None:
        if node_id not in self.visible:
            return None
        detail = self.describe(node_id)
        detail["path"] = self.path_labels(node_id)
        detail["parents"] = [
            parent
            for parent in self.artifact.nodes[node_id].parents
            if parent in self.visible
        ]
        return detail

    def describe(self, node_id: str) -> dict:
        node = self.artifact.nodes[node_id]
        child_count = sum(
            1 for c in self.artifact.children.get(node_id, ()) if c in self.visible
        )
        payload = {"id": node.id, "label": node.label, "children": child_count}
        if node.definition:
            payload["definition"] = node.definition
        if node.synonyms:
            payload["synonyms"] = list(node.synonyms)
        return payload

    # -- closure, paths, resolution -----------------------------------------

    def ancestors(self, node_id: str) -> frozenset[str]:
        """All visible ancestors including the node itself (DAG-safe).

        This set is what documents/chunks store and what subtree filters match
        against: filtering by any node matches every document whose ancestor
        set contains it.
        """
        if node_id in self._ancestor_cache:
            return self._ancestor_cache[node_id]
        if node_id not in self.visible:
            return frozenset()
        closure: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in closure:
                continue
            closure.add(current)
            for parent in self.artifact.nodes[current].parents:
                if parent in self.visible:
                    stack.append(parent)
        result = frozenset(closure)
        self._ancestor_cache[node_id] = result
        return result

    def path_labels(self, node_id: str) -> list[str]:
        """Display path via the first visible parent chain (root -> node)."""
        path = [node_id]
        current = node_id
        seen = {node_id}
        while True:
            parents = [
                p
                for p in self.artifact.nodes[current].parents
                if p in self.visible and p not in seen
            ]
            if not parents:
                break
            current = parents[0]
            seen.add(current)
            path.append(current)
        return [self.artifact.nodes[p].label for p in reversed(path)]

    def resolve(self, node_id: str | None) -> str | None:
        """Map a node id to its visible representative.

        Visible ids pass through. An id that exists in the artifact but is
        currently hidden (disabled subtree / inactive facet) falls back to its
        nearest visible ancestor. Unknown ids resolve to None.
        """
        if not node_id:
            return None
        if node_id in self.visible:
            return node_id
        node = self.artifact.nodes.get(node_id)
        if node is None:
            return None
        stack = list(node.parents)
        seen: set[str] = set()
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if current in self.visible:
                return current
            parent = self.artifact.nodes.get(current)
            if parent is not None:
                stack.extend(parent.parents)
        return None

    def label_of(self, node_id: str) -> str | None:
        node = self.artifact.nodes.get(node_id)
        return node.label if node else None

    def depth_of(self, node_id: str) -> int:
        return max(0, len(self.path_labels(node_id)) - 1)

    def indented_menu(self, *, max_depth: int = 2) -> str:
        """Compact 'id  label' menu of the whole visible facet, indented by depth.

        For SHALLOW facets only (Area of Law: 161 nodes) where a menu in the
        prompt beats an agentic walk — no definitions, no extra rounds. Never
        use for the document-type facet (1,400+ nodes)."""
        lines: list[str] = []

        def walk(node_id: str, depth: int) -> None:
            node = self.artifact.nodes[node_id]
            lines.append(f"{'  ' * depth}{node.id}  {node.label}")
            if depth < max_depth:
                for child in self.artifact.children.get(node_id, ()):
                    if child in self.visible:
                        walk(child, depth + 1)

        for root in self._root_ids:
            walk(root, 0)
        return "\n".join(lines)

    # -- lexical search: retrieval-time filter discovery only ----------------

    def search(self, query: str, *, limit: int | None = 12) -> list[dict]:
        """Deterministic lexical search over the visible set.

        Ranking: exact label > label prefix > label substring > synonym
        substring > definition substring; ties broken alphabetically. No
        embeddings, no model calls — same query, same result, always.

        ``limit=None`` returns every match. The whole artifact is already in
        memory, so a caller that wants to page the matches (and report how many
        there were) does not pay for the completeness.
        """
        needle = " ".join(query.lower().split())
        if not needle:
            return []
        scored: list[tuple[int, str, str]] = []
        for node_id in self.visible:
            node = self.artifact.nodes[node_id]
            label = node.label.lower()
            score = 0
            if label == needle:
                score = 100
            elif label.startswith(needle):
                score = 80
            elif needle in label:
                score = 60
            elif any(needle in synonym.lower() for synonym in node.synonyms):
                score = 40
            elif needle in node.definition.lower():
                score = 20
            if score:
                scored.append((-score, node.label, node_id))
        scored.sort()
        results = []
        for _neg, _label, node_id in (scored if limit is None else scored[:limit]):
            detail = self.describe(node_id)
            detail["path"] = self.path_labels(node_id)
            results.append(detail)
        return results


# -- artifact discovery and cached loading -----------------------------------


def _packaged_artifacts() -> dict[str, Path]:
    root = files("knowledge_index") / PACKAGED_DATA
    found: dict[str, Path] = {}
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return found
    for entry in entries:
        name = entry.name
        if name.endswith(".json.gz") or name.endswith(".json"):
            found[name.split(".json")[0]] = Path(str(entry))
    return found


def discover_artifacts(uploads_dir: Path | None = None) -> dict[str, Path]:
    """Artifacts by name: packaged defaults, overridden by uploaded files."""
    found = _packaged_artifacts()
    if uploads_dir is not None and uploads_dir.is_dir():
        for entry in sorted(uploads_dir.iterdir()):
            if entry.name.endswith((".json.gz", ".json")):
                found[entry.name.split(".json")[0]] = entry
    return found


@lru_cache(maxsize=8)
def _load_artifact_cached(path: str, mtime_ns: int) -> OntologyArtifact:
    del mtime_ns  # part of the cache key only
    return OntologyArtifact.parse(Path(path).read_bytes())


def load_artifact(path: Path) -> OntologyArtifact:
    return _load_artifact_cached(str(path), path.stat().st_mtime_ns)


@lru_cache(maxsize=16)
def _scope_cached(
    path: str,
    mtime_ns: int,
    active_facets: tuple[str, ...],
    disabled_nodes: frozenset[str],
) -> OntologyScope:
    return OntologyScope(
        _load_artifact_cached(path, mtime_ns), active_facets, disabled_nodes
    )


def ontology_scope(
    artifact_path: Path,
    active_facets: Iterable[str] = ("doc_type",),
    disabled_nodes: Iterable[str] = (),
) -> OntologyScope:
    """Resolve the scoped ontology view. Cheap to call per task: cached on
    (artifact file, facets, disabled set), so a mid-run scope change takes
    effect for the next task without restarts."""
    return _scope_cached(
        str(artifact_path),
        artifact_path.stat().st_mtime_ns,
        tuple(active_facets),
        frozenset(disabled_nodes),
    )
