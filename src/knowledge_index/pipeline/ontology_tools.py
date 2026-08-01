"""Deterministic ontology-navigation tools for the metadata extraction agent.

Pure lookups over the scoped ontology — no search, no side effects, no
database. Every node id returned by any call is recorded in ``visited``; the
stage's result validator only accepts a ``type_node`` from that set, so the
model can never submit an id it has not actually seen (the same discipline as
the relate stage's opened-refs rule).
"""

from __future__ import annotations

import json

from knowledge_index.ontology import OntologyScope
from knowledge_index.pipeline.providers import AgentTool


def _facet_handlers(scope: OntologyScope, visited: set[str]) -> dict:
    def roots(_args: dict) -> str:
        nodes = scope.roots()
        visited.update(node["id"] for node in nodes)
        return json.dumps(nodes, ensure_ascii=False)

    def children(args: dict) -> str:
        node_id = str(args.get("node_id", ""))
        if node_id not in scope.visible:
            return json.dumps({"error": f"unknown or inactive node {node_id!r}"})
        visited.add(node_id)
        nodes = scope.children(node_id)
        visited.update(node["id"] for node in nodes)
        return json.dumps(
            {"node": scope.label_of(node_id), "children": nodes}, ensure_ascii=False
        )

    def node(args: dict) -> str:
        node_id = str(args.get("node_id", ""))
        detail = scope.node(node_id)
        if detail is None:
            return json.dumps({"error": f"unknown or inactive node {node_id!r}"})
        visited.add(node_id)
        return json.dumps(detail, ensure_ascii=False)

    def search(args: dict) -> str:
        results = scope.search(str(args.get("query", "")), limit=12)
        visited.update(item["id"] for item in results)
        return json.dumps(results, ensure_ascii=False)

    return {"roots": roots, "children": children, "node": node, "search": search}


def ontology_navigation_tools(scope: OntologyScope, visited: set[str]) -> list[AgentTool]:
    handlers = _facet_handlers(scope, visited)
    roots, children, node, search = (
        handlers["roots"],
        handlers["children"],
        handlers["node"],
        handlers["search"],
    )
    return [
        AgentTool(
            name="ontology_search",
            description=(
                "Find nodes anywhere in the active ontology by label, synonym, or "
                "definition. Deterministic lexical ranking — the same query always "
                "returns the same results, each with its full path. Search for what "
                "the document IS (its form), not its subject matter."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=search,
        ),
        AgentTool(
            name="ontology_roots",
            description=(
                "The top-level branches of the active document-type ontology. Start "
                "here, then descend with ontology_children."
            ),
            parameters={"type": "object", "properties": {}},
            handler=roots,
        ),
        AgentTool(
            name="ontology_children",
            description=(
                "A node's children with their definitions. Descend one level at a "
                "time; a child count of 0 marks a leaf."
            ),
            parameters={
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
            handler=children,
        ),
        AgentTool(
            name="ontology_node",
            description="Full detail for one node: definition, synonyms, path, parents.",
            parameters={
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
            handler=node,
        ),
    ]


def service_navigation_tools(scope: OntologyScope, visited: set[str]) -> list[AgentTool]:
    """The classify agent's view of the Service facet — same handlers and visited
    discipline as the document-type tools, service-flavored names and guidance so
    the model judges kinds of WORK by definition, not by label sound."""
    handlers = _facet_handlers(scope, visited)
    return [
        AgentTool(
            name="service_search",
            description=(
                "Find kind-of-service nodes (what the firm is DOING) by label, "
                "synonym, or definition. Deterministic lexical ranking, full path "
                "per result. Search for the work performed, never the subject or "
                "industry — those belong in the area of law."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=handlers["search"],
        ),
        AgentTool(
            name="service_children",
            description=(
                "A service node's children with their definitions. Descend one "
                "level at a time from the top-level kinds of engagement."
            ),
            parameters={
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
            handler=handlers["children"],
        ),
        AgentTool(
            name="service_node",
            description=(
                "Full detail for one service node: definition, synonyms, path. "
                "ALWAYS read a node's definition before submitting it — labels are "
                "misleading."
            ),
            parameters={
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
            handler=handlers["node"],
        ),
        AgentTool(
            name="service_roots",
            description="The top-level kinds of engagement in the service facet.",
            parameters={"type": "object", "properties": {}},
            handler=handlers["roots"],
        ),
    ]


def clause_search_tool(scope: OntologyScope, visited: set[str]) -> AgentTool:
    """Search over the clause-type facet — a flat vocabulary where navigation is
    pointless and lookup by conventional name is the whole game. Same visited
    discipline as the document-type tools: a clause_type_node is only accepted
    if some clause_search result contained it."""

    def search(args: dict) -> str:
        results = scope.search(str(args.get("query", "")), limit=8)
        visited.update(item["id"] for item in results)
        return json.dumps(results, ensure_ascii=False)

    return AgentTool(
        name="clause_search",
        description=(
            "Find clause-type nodes by the clause's conventional English name "
            "('governing law', 'force majeure'). Deterministic lexical ranking. "
            "This vocabulary is separate from the document-type tree — ids from "
            "here are only valid as clause_type_node values."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=search,
    )
