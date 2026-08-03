"""Real-usage benchmark: consume the RAG the way a client actually does.

Four consumption modes over the same corpus, so task success (the task-set rubric) is
comparable across them:

- ``closed_book`` — the task, no retrieval; answers from parametric knowledge only.
- ``classic_rag`` — one-shot: embed the task, take top-k chunks, stuff them into the
  prompt, generate once. "Normal retrieval without tools" — the standard RAG baseline.
- ``agentic`` — the LLM composes the real tool suite (``search_filter`` →
  ``search_semantic`` → ``traverse`` → ``get_document``) in a loop, then drafts. The
  target: how this system is designed to be used.
- ``oracle`` — the gold documents are handed in directly (the task set's native setup): the
  retrieval ceiling, isolating generation from retrieval.

The agentic mode drives the **real MCP server** (``mcp_server.create_mcp_server``)
in-process under a fixed principal — one source of truth for the tool surface, ACL /
ethical-wall enforcement, and audit that external clients get. Every document a mode
surfaces is recorded (``retrieved_paths``) for context-recall scoring. All LLM calls go
through the LiteLLM gateway.
"""

from __future__ import annotations

import asyncio
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
    # full observability, collected on every run:
    trajectory: list[dict] = field(default_factory=list)  # each tool call: name, args, results
    messages: list[dict] = field(default_factory=list)  # the raw model conversation
    usage: dict = field(default_factory=dict)  # prompt/completion/total tokens + call count


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
    """Sync facade over the real MCP server — the single source of truth for the tools.

    The agentic mode drives the exact tool surface external clients get
    (``mcp_server.create_mcp_server``): names, descriptions, JSON schemas, ACL and audit
    all come from one place. The vendored agent loop hands us ``(name, arguments)``; we
    dispatch to the MCP tool's raw callable with the caller principal injected as the
    trusted-header identity, and record every authorized source path for context recall.
    """

    def __init__(self, service: RetrievalService, principal: str) -> None:
        from sqlalchemy.orm import sessionmaker

        from knowledge_index.mcp_server import create_mcp_server

        # MCP tools open their own sessions; bind a factory to the service's engine.
        session_factory = sessionmaker(service.session.get_bind())
        self._server = create_mcp_server(session_factory, lambda: service.config)
        self._headers = {"x-ki-principals": principal}
        self._tools = {tool.name: tool for tool in asyncio.run(self._server.list_tools())}
        # the server's own usage guidance — a real MCP client incorporates this into the
        # system prompt (MCP spec: InitializeResult.instructions), so the benchmark does too.
        self.instructions: str = self._server.instructions or ""
        self.retrieved_paths: set[str] = set()
        self.calls = 0
        self.trace: list[dict] = []  # one record per tool call: name, args, results surfaced

    def specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def get_metrics(self) -> dict:
        return {"tool_calls": self.calls, "documents_retrieved": len(self.retrieved_paths)}

    def execute(self, name: str, arguments: str | dict) -> str:
        # The vendored agent loop passes tool arguments as a JSON string
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        self.calls += 1
        tool = self._tools.get(name)
        if tool is None:
            self.trace.append({"tool": name, "args": args, "error": "unknown tool"})
            return json.dumps({"error": f"unknown tool {name}"})
        before = set(self.retrieved_paths)
        try:
            # the raw closure; principal injected via the trusted-identity header
            result = tool.fn(**args, headers=self._headers)
        except Exception as exc:  # a tool error is fed back to the agent, not fatal
            self.trace.append({"tool": name, "args": args, "error": f"{type(exc).__name__}: {exc}"})
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        self._track_paths(result)
        # record the full trajectory step: the call, its args, and what it surfaced
        n_results = len(result) if isinstance(result, list) else (0 if result is None else 1)
        self.trace.append({
            "tool": name, "args": args, "n_results": n_results,
            "surfaced_paths": sorted(self.retrieved_paths - before),
        })
        payload = json.dumps(result, ensure_ascii=False, default=str)
        # Cap a whole-document dump: a credit agreement can exceed the model's context
        # window and hard-400 the loop. The agent can search_semantic for the passage.
        if name == "get_document" and len(payload) > 30000:
            payload = payload[:30000] + "… [truncated; search_semantic for the exact passage]"
        return payload

    def _track_paths(self, result: object) -> None:
        """Record every authorized source path a result surfaced (for context recall)."""
        for item in result if isinstance(result, list) else [result]:
            if not isinstance(item, dict):
                continue
            for path in item.get("source_paths") or []:
                self.retrieved_paths.add(path)
            for source in item.get("sources") or []:
                if isinstance(source, dict) and source.get("path"):
                    self.retrieved_paths.add(source["path"])


# ------------------------------------------------------------------------ consume modes


def run_closed_book(task: Task, config: AppConfig, model: str) -> ProducedWork:
    usage: dict = {}
    messages = [
        {"role": "system", "content": CLOSED_BOOK_SYSTEM},
        {"role": "user", "content": _output_spec(task)},
    ]
    message = gateway.complete(config, model, messages, usage_sink=usage)
    return ProducedWork(
        "closed_book", message.get("content") or "", llm_calls=1,
        messages=[*messages, message], usage=usage,
    )


def run_classic_rag(
    task: Task, service: RetrievalService, config: AppConfig, model: str, *, k: int = 10
) -> ProducedWork:
    hits = service.search_semantic(
        task.instruction, principals={task.principal}, filters=SearchFilters(), limit=k
    )
    retrieved = {path for hit in hits for path in hit.source_paths}
    trajectory = [{
        "tool": "search_semantic", "args": {"query": task.instruction, "limit": k},
        "n_results": len(hits), "surfaced_paths": sorted(retrieved),
    }]
    context = "\n\n".join(
        f"[{hit.title or hit.doc_type} — {', '.join(hit.source_paths)}]\n{hit.excerpt}"
        for hit in hits
    )
    usage: dict = {}
    messages = [
        {"role": "system", "content": CLASSIC_SYSTEM},
        {"role": "user", "content": f"RETRIEVED CONTEXT:\n{context}\n\n{_output_spec(task)}"},
    ]
    message = gateway.complete(config, model, messages, usage_sink=usage)
    return ProducedWork(
        "classic_rag", message.get("content") or "", retrieved, len(hits), 1,
        trajectory=trajectory, messages=[*messages, message], usage=usage,
    )


def run_oracle(
    task: Task, gold_texts: dict[str, str], config: AppConfig, model: str
) -> ProducedWork:
    context = "\n\n".join(f"[{path}]\n{text[:6000]}" for path, text in gold_texts.items())
    trajectory = [{"tool": "oracle_provided", "surfaced_paths": sorted(gold_texts)}]
    usage: dict = {}
    messages = [
        {"role": "system", "content": CLASSIC_SYSTEM},
        {"role": "user", "content": f"PROVIDED DOCUMENTS:\n{context}\n\n{_output_spec(task)}"},
    ]
    message = gateway.complete(config, model, messages, usage_sink=usage)
    return ProducedWork(
        "oracle", message.get("content") or "", set(gold_texts), 0, 1,
        trajectory=trajectory, messages=[*messages, message], usage=usage,
    )


def _incorporate_server_instructions(system: str, instructions: str) -> str:
    """Layer the MCP server's own usage guidance onto the agent's system prompt.

    Per the MCP spec a client MAY incorporate a server's ``InitializeResult.instructions``
    into the system prompt as a hint. We keep the benchmark's role/task prompt first, then
    append the server's tool-usage guidance — exactly what a real MCP client does.
    """
    if not instructions.strip():
        return system
    return f"{system}\n\n# Knowledge index (MCP) server guidance\n{instructions}"


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
    system = _incorporate_server_instructions(system, suite.instructions)
    adapter = GatewayAdapter(config, model)
    result = run_agent(
        adapter,
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
        "agentic", work_product, suite.retrieved_paths, suite.calls, result["turn_count"],
        trajectory=suite.trace, messages=result["messages"], usage=adapter.usage,
    )
