"""Real-usage benchmark: consume the RAG the way a client actually does.

Four consumption modes over the same corpus, so task success (the rubric) is
comparable across them:

- ``closed_book`` — the task, no retrieval; answers from parametric knowledge only.
- ``classic_rag`` — one-shot: embed the task, take top-k chunks, stuff them into the
  prompt, generate once. "Normal retrieval without tools" — the standard RAG baseline.
- ``agentic`` — the LLM composes the real tool suite (``search_filter`` →
  ``search_semantic`` → ``traverse`` → ``get_document``) in a loop, then drafts. The
  target: how this system is designed to be used.
- ``oracle`` — the gold documents are handed in directly (the upstream harness's native setup): the
  retrieval ceiling, isolating generation from retrieval.

Tools execute in-process against the same ``RetrievalService`` the MCP server wraps,
under a fixed principal, so ACL / ethical-wall enforcement is real. Every document a
mode surfaces is recorded (``retrieved_paths``) for context-recall scoring. All LLM
calls go through the LiteLLM gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from knowledge_index.benchmark import gateway
from knowledge_index.config import AppConfig
from knowledge_index.retrieval_types import SearchFilters

if TYPE_CHECKING:
    # retrieval (pgvector chain) kept out of module import so the pure tool-suite surface
    # stays offline-testable; the live service is passed in, gateway calls go via gateway
    from knowledge_index.retrieval import RetrievalService

AGENT_SYSTEM = (
    "You are a legal associate at a firm. Complete the assignment using ONLY facts you "
    "retrieve from the firm's knowledge index via the provided tools — do not invent "
    "parties, numbers, or terms. Search first (filter by matter/type when known, then "
    "semantic search), open the documents you need, then produce the requested work "
    "product in full. When done, reply with the finished work product as plain text."
)
CLASSIC_SYSTEM = (
    "You are a legal associate. Using only the retrieved context below, produce the "
    "requested work product in full. Do not invent parties, numbers, or terms."
)
CLOSED_BOOK_SYSTEM = (
    "You are a legal associate. Produce the requested work product as best you can."
)


@dataclass
class ProducedWork:
    mode: str
    work_product: str
    retrieved_paths: set[str] = field(default_factory=set)
    tool_calls: int = 0
    llm_calls: int = 0


@dataclass
class Task:
    scenario_id: str
    instruction: str
    criteria: list[dict]
    gold_paths: list[str]
    principal: str
    matter_ref: str


def _output_spec(task: Task) -> str:
    return f"ASSIGNMENT:\n{task.instruction}"


# --------------------------------------------------------------------------- tool suite


class ToolSuite:
    """The MCP tool surface, executed in-process against RetrievalService."""

    def __init__(self, service: RetrievalService, principal: str) -> None:
        self.service = service
        self.principals = {principal}
        self.retrieved_paths: set[str] = set()
        self.calls = 0

    def specs(self) -> list[dict]:
        def tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }

        filters = {
            "matter_id": {"type": "string"},
            "doc_type": {"type": "string"},
            "version_status": {"type": "string"},
        }
        return [
            tool(
                "search_filter",
                "List documents by exact metadata (matter, doc_type, status). Use first "
                "when the matter or type is known.",
                filters,
                [],
            ),
            tool(
                "search_semantic",
                "Hybrid semantic + lexical search over document chunks; accepts the same "
                "metadata pre-filters.",
                {"query": {"type": "string"}, **filters},
                ["query"],
            ),
            tool(
                "list_matters",
                "List matters visible to you, with reference numbers and practice area.",
                {},
                [],
            ),
            tool(
                "get_document",
                "Fetch one document's full structured content by document_id.",
                {"document_id": {"type": "string"}},
                ["document_id"],
            ),
            tool(
                "traverse",
                "Walk typed relations (version chain, annexes, thread) from a document_id.",
                {"document_id": {"type": "string"}},
                ["document_id"],
            ),
        ]

    def get_metrics(self) -> dict:
        return {"tool_calls": self.calls, "documents_retrieved": len(self.retrieved_paths)}

    def execute(self, name: str, arguments: str | dict) -> str:
        # The vendored agent loop passes tool arguments as a JSON string
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        self.calls += 1
        try:
            if name == "search_filter":
                hits = self.service.search_filter(
                    principals=self.principals, filters=_filters(args), limit=20
                )
                return self._record(hits)
            if name == "search_semantic":
                hits = self.service.search_semantic(
                    args.get("query", ""),
                    principals=self.principals,
                    filters=_filters(args),
                    limit=20,
                )
                return self._record(hits)
            if name == "list_matters":
                matters = self.service.list_matters(principals=self.principals)
                return json.dumps(matters, ensure_ascii=False)
            if name == "get_document":
                doc = self.service.get_document(args["document_id"], principals=self.principals)
                for source in (doc or {}).get("sources", []):
                    self.retrieved_paths.add(source["path"])
                # no truncation: get_document is the "read the whole document" tool
                return json.dumps(doc, ensure_ascii=False)
            if name == "traverse":
                rows = self.service.traverse(
                    "document", args["document_id"], principals=self.principals
                )
                return json.dumps(rows, ensure_ascii=False)[:4000]
        except Exception as exc:  # a tool error is fed back to the agent, not fatal
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"error": f"unknown tool {name}"})

    def _record(self, hits: list) -> str:
        for hit in hits:
            self.retrieved_paths.update(hit.source_paths)
        payload = [
            {
                "document_id": hit.document_id,
                "title": hit.title,
                "doc_type": hit.doc_type,
                "version_status": hit.version_status,
                "excerpt": hit.excerpt,
                "source_paths": hit.source_paths,
            }
            for hit in hits
        ]
        return json.dumps(payload, ensure_ascii=False)[:8000]


def _filters(args: dict) -> SearchFilters:
    return SearchFilters(
        matter_id=args.get("matter_id"),
        doc_type=args.get("doc_type"),
        version_status=args.get("version_status"),
    )


# ------------------------------------------------------------------------ consume modes


def run_closed_book(task: Task, config: AppConfig, model: str) -> ProducedWork:
    message = gateway.complete(
        config,
        model,
        [
            {"role": "system", "content": CLOSED_BOOK_SYSTEM},
            {"role": "user", "content": _output_spec(task)},
        ],
    )
    return ProducedWork("closed_book", message.get("content") or "", llm_calls=1)


def run_classic_rag(
    task: Task, service: RetrievalService, config: AppConfig, model: str, *, k: int = 10
) -> ProducedWork:
    hits = service.search_semantic(
        task.instruction, principals={task.principal}, filters=SearchFilters(), limit=k
    )
    retrieved = {path for hit in hits for path in hit.source_paths}
    context = "\n\n".join(
        f"[{hit.title or hit.doc_type} — {', '.join(hit.source_paths)}]\n{hit.excerpt}"
        for hit in hits
    )
    message = gateway.complete(
        config,
        model,
        [
            {"role": "system", "content": CLASSIC_SYSTEM},
            {"role": "user", "content": f"RETRIEVED CONTEXT:\n{context}\n\n{_output_spec(task)}"},
        ],
    )
    return ProducedWork("classic_rag", message.get("content") or "", retrieved, len(hits), 1)


def run_oracle(
    task: Task, gold_texts: dict[str, str], config: AppConfig, model: str
) -> ProducedWork:
    context = "\n\n".join(f"[{path}]\n{text[:6000]}" for path, text in gold_texts.items())
    message = gateway.complete(
        config,
        model,
        [
            {"role": "system", "content": CLASSIC_SYSTEM},
            {"role": "user", "content": f"PROVIDED DOCUMENTS:\n{context}\n\n{_output_spec(task)}"},
        ],
    )
    return ProducedWork("oracle", message.get("content") or "", set(gold_texts), 0, 1)


def run_agentic(
    task: Task,
    service: "RetrievalService",
    config: AppConfig,
    model: str,
    *,
    max_steps: int = 25,
    system: str = AGENT_SYSTEM,
) -> ProducedWork:
    """Drive the vendored agent loop over our retrieval tool suite (no sandbox)."""
    from knowledge_index.benchmark.gateway_adapter import GatewayAdapter
    from knowledge_index.benchmark.agent_harness.agent_loop import run_agent

    suite = ToolSuite(service, task.principal)
    result = run_agent(
        GatewayAdapter(config, model),
        system,
        _output_spec(task),
        suite,
        tools=suite.specs(),
        max_turns=max_steps,
    )
    work_product = ""
    for message in reversed(result["messages"]):
        if message.get("role") == "assistant" and message.get("content"):
            work_product = message["content"]
            break
    return ProducedWork(
        "agentic", work_product, suite.retrieved_paths, suite.calls, result["turn_count"]
    )
