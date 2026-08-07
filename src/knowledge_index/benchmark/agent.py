"""Agent-side machinery for the agentic benchmark: the tool facade and run modes.

Two ways to consume the RAG, matching how clients actually use it:

- ``run_classic_rag`` — one-shot: retrieve top-k for the query, stuff it into the
  prompt, generate once. The standard non-agentic RAG baseline.
- ``run_agentic`` — the LLM composes the real tool suite in a loop (the vendored
  ``agent_loop``, MIT) until it answers.

The agentic mode drives the **real MCP server** (``mcp_server.create_mcp_server``)
in-process under a fixed principal — one source of truth for the tool surface, ACL /
ethical-wall enforcement, and audit that external clients get. ``ToolSuite`` takes an
``allowed_tools`` allowlist so the agentic matrix can ablate the tool surface
(search-only → +filters → everything). Every document a mode surfaces is recorded
(``retrieved_paths``) for context-recall scoring. All LLM calls go through the
LiteLLM gateway.
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
    # retrieval kept out of module import so the pure tool-suite surface stays
    # offline-testable; the live service is passed in, gateway calls go via gateway
    from knowledge_index.retrieval import RetrievalService

# The working protocol is the LegalMemory demo's, kept step-for-step so the benchmark
# measures the harness the product actually ships rather than one written for it. The
# demo's UI sections (result cards, the Sources card, the [[doc:id|title]] citation
# format the interface resolves) are dropped — there is no interface here — and the
# citation requirement is restated in a form a grader can read.
AGENT_SYSTEM = """You are a legal knowledge assistant. You answer questions about the documents indexed in this firm's knowledge index, and about nothing else.

Every fact you state about a document must come from a tool call in this conversation.

## How to work

Follow this order. Most wrong answers come from skipping a step, not from a bad search.

1. **Search broadly once.** One search_semantic with no filters, to find out where the answer lives.
2. **Narrow to the matter.** The hits carry matter_id. Once you know which matter the question is about, search again with that matter_id — a filtered search is the difference between the firm's documents and this matter's documents, and it is what makes an answer about "the Hargrove acquisition" actually about Hargrove. Use search_filter with the matter_id and no query to see everything in it.
3. **Read what you found.** A hit carries the whole chunk that matched, which is one passage of a document, not the document. What you are asked about is often in a different passage — a memorandum names counsel for both sides in one paragraph and the deal terms twenty pages later. Treat a hit as evidence the document is relevant, then get_document the candidates before you answer from them.
4. **Traverse before you conclude.** Call find_related_documents on the documents you are relying on. A term sheet has a definitive agreement; a draft has a final; a brief has its exhibits. Those links are stored with evidence and a search will not recover them.

Step 4 matters most when you are about to say something is *absent*. "The agreement is not in the index" is a strong claim, and traversing the relations of what you did find is how you check it rather than assume it.

Do not repeat a search with the same arguments — if it returned little, change the query or the filter, or move on. Do not call list_matters to orient yourself mid-investigation; the hits already told you the matter.

Results are already restricted to the identity this session is signed in as. If a search returns nothing, say so plainly — it means the documents are not there or not readable by this user, and both are real answers. Never guess at the contents of a document you did not read.

## Answering

Give a short, direct answer to what was asked, and name the document you took it from — its title or path, verbatim from the tool result. A claim without a document behind it is not an answer; if you cannot point to one, do not assert it.

Do not offer conclusions the documents do not support. If the index does not contain the answer, say so."""


def traverse_once(calls_so_far: list[str]) -> dict | None:
    """Force one relation traversal, rather than asking for it.

    Ported from the demo harness. The instruction to traverse before concluding is
    in the prompt, and a small model ignores it — in this benchmark
    ``find_related_documents`` fired 10 times in 1,282 runs. Stored relations are
    the part of the index a search cannot reproduce, so an answer that skips them
    is an answer from search alone.

    Once the model has read a document and is still deciding what to do next, one
    traversal is pinned. Once only: after that it chooses freely again, and a model
    that wanted to traverse anyway is unaffected because the guard sees its call
    and stands down.
    """
    if "find_related_documents" in calls_so_far:
        return None
    if "get_document" not in calls_so_far:
        return None
    return {"type": "function", "function": {"name": "find_related_documents"}}
CLASSIC_SYSTEM = (
    "You are a legal knowledge assistant. Using only the retrieved context below, give "
    "a SHORT, direct answer to the request and cite the document it came from. Do not "
    "invent anything; if the context does not contain the answer, say so."
)


@dataclass
class AgentResult:
    mode: str
    answer: str
    retrieved_paths: set[str] = field(default_factory=set)
    tool_calls: int = 0
    llm_calls: int = 0
    # full observability, collected on every run:
    trajectory: list[dict] = field(default_factory=list)  # each tool call: name, args, results
    messages: list[dict] = field(default_factory=list)  # the raw model conversation
    usage: dict = field(default_factory=dict)  # prompt/completion/total tokens + call count


# --------------------------------------------------------------------------- tool suite


class ToolSuite:
    """Sync facade over the real MCP server — the single source of truth for the tools.

    The agentic mode drives the exact tool surface external clients get
    (``mcp_server.create_mcp_server``): names, descriptions, JSON schemas, ACL and audit
    all come from one place. The vendored agent loop hands us ``(name, arguments)``; we
    dispatch to the MCP tool's raw callable with the caller principal injected as the
    trusted-header identity, and record every authorized source path for context recall.

    ``allowed_tools`` restricts the surface (the agentic matrix's tool ablation);
    ``None`` exposes everything. An unknown allowlisted name fails loud — a config typo
    must not silently benchmark the wrong surface.
    """

    def __init__(
        self,
        service: RetrievalService,
        principal: str,
        *,
        allowed_tools: set[str] | None = None,
    ) -> None:
        from sqlalchemy.orm import sessionmaker

        from knowledge_index.mcp_server import create_mcp_server

        # MCP tools open their own sessions; bind a factory to the service's engine.
        session_factory = sessionmaker(service.session.get_bind())
        self._server = create_mcp_server(session_factory, lambda: service.config)
        self._headers = {"x-ki-principals": principal}
        self._tools = {tool.name: tool for tool in asyncio.run(self._server.list_tools())}
        if allowed_tools is not None:
            unknown = allowed_tools - set(self._tools)
            if unknown:
                raise KeyError(f"allowed_tools not on the MCP surface: {sorted(unknown)}")
            self._tools = {
                name: tool for name, tool in self._tools.items() if name in allowed_tools
            }
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
        # Validate against the tool's own JSON schema before dispatch — a real MCP
        # client gets a clean validation error, never a raw Python TypeError leaking
        # harness internals into the model's context.
        schema = tool.parameters or {}
        properties = schema.get("properties") or {}
        missing = [field for field in schema.get("required", []) if field not in args]
        unknown = [key for key in args if properties and key not in properties]
        if missing or unknown:
            parts = []
            if missing:
                parts.append(f"missing required argument(s): {', '.join(missing)}")
            if unknown:
                parts.append(f"unknown argument(s): {', '.join(unknown)}")
            error = f"invalid arguments for {name}: {'; '.join(parts)}"
            self.trace.append({"tool": name, "args": args, "error": error})
            return json.dumps({"error": error})
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
        """Record every authorized source path a result surfaced (for context recall).

        Evidence reaches a client in several shapes: search hits carry
        ``source_paths``; every other evidence-bearing tool carries the citation
        contract (``citations[].source_objects[].path``), and graph results nest
        citations inside edges. A walker that knew only the search-hit shape made
        ``get_document``, ``resolve_entity``, ``traverse`` and friends invisible to
        the metric — which punished exactly the configs that used the richer tool
        surface. So walk the whole payload.

        A source object is identified by the source-reference contract (a ``path``
        alongside ``source_id``/``external_id``), never by a bare ``path`` key —
        unrelated fields (e.g. an ontology label path) must not count as evidence.
        """
        stack: list = [result]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for path in node.get("source_paths") or []:
                    if isinstance(path, str) and path:
                        self.retrieved_paths.add(path)
                path = node.get("path")
                if (
                    isinstance(path, str)
                    and path
                    and ("source_id" in node or "external_id" in node)
                ):
                    self.retrieved_paths.add(path)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


# ------------------------------------------------------------------------ consume modes


def run_classic_rag(
    query: str,
    principal: str,
    service: RetrievalService,
    config: AppConfig,
    model: str,
    *,
    k: int = 10,
) -> AgentResult:
    hits = service.search_semantic(
        query, principals={principal}, filters=SearchFilters(), limit=k
    )
    retrieved = {path for hit in hits for path in hit.source_paths}
    trajectory = [{
        "tool": "search_semantic", "args": {"query": query, "limit": k},
        "n_results": len(hits), "surfaced_paths": sorted(retrieved),
    }]
    context = "\n\n".join(
        f"[{hit.title or hit.doc_type} — {', '.join(hit.source_paths)}]\n{hit.text}"
        for hit in hits
    )
    usage: dict = {}
    messages = [
        {"role": "system", "content": CLASSIC_SYSTEM},
        {"role": "user", "content": f"RETRIEVED CONTEXT:\n{context}\n\nREQUEST:\n{query}"},
    ]
    message = gateway.complete(config, model, messages, usage_sink=usage)
    # one retrieval call, one generation: tool_calls is 1, NOT len(hits) — the hit
    # count belongs in the trajectory, and conflating them made the one-shot baseline
    # look like the most tool-hungry config in the matrix.
    return AgentResult(
        "classic_rag", message.get("content") or "", retrieved, 1, 1,
        trajectory=trajectory, messages=[*messages, message], usage=usage,
    )


def gold_document_texts(session, gold_paths: list[str]) -> dict[str, str]:
    """Converted text for each gold document — the oracle's hand-delivered context."""
    from sqlalchemy import select

    from knowledge_index.db.models import Artifact, SourceObject

    texts: dict[str, str] = {}
    for path in gold_paths:
        source = session.scalar(select(SourceObject).where(SourceObject.path == path))
        if source is None:
            continue
        artifact = session.scalar(
            select(Artifact)
            .where(
                Artifact.content_hash == source.content_hash,
                Artifact.kind == "structured_json",
            )
            .order_by(Artifact.created_at.desc())
        )
        text = (artifact.payload or {}).get("text") if artifact else None
        if text:
            texts[path] = text
    return texts


def run_oracle(
    query: str,
    gold_texts: dict[str, str],
    config: AppConfig,
    model: str,
    *,
    primary: set[str] | None = None,
    char_budget: int = 60000,
) -> AgentResult:
    """Answer with the gold documents handed in — retrieval removed from the equation.

    This is the ceiling: whatever it misses, no retrieval improvement can fix, because
    the right documents were already in the prompt. The gap between it and the shipped
    system is the part of the error budget that belongs to retrieval; the gap between
    it and 100% belongs to reading and generation.

    The budget is filled *primary document first*. Splitting it evenly is what broke
    the previous oracle: gold averages ~2.5 documents, so the one document holding the
    answer arrived cut to its cover page and table of contents, and the oracle failed
    for lack of context rather than for lack of comprehension — understating the very
    ceiling it exists to establish.
    """
    ordered = sorted(gold_texts, key=lambda path: path not in (primary or set()))
    remaining = char_budget
    parts = []
    for path in ordered:
        if remaining <= 0:
            break
        text = gold_texts[path][:remaining]
        remaining -= len(text)
        parts.append(f"[{path}]\n{text}")
    context = "\n\n".join(parts)
    usage: dict = {}
    messages = [
        {"role": "system", "content": CLASSIC_SYSTEM},
        {"role": "user", "content": f"PROVIDED DOCUMENTS:\n{context}\n\nREQUEST:\n{query}"},
    ]
    message = gateway.complete(config, model, messages, usage_sink=usage)
    return AgentResult(
        "oracle",
        message.get("content") or "",
        set(gold_texts),
        0,
        1,
        trajectory=[{"tool": "oracle_provided", "surfaced_paths": sorted(gold_texts)}],
        messages=[*messages, message],
        usage=usage,
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
    query: str,
    principal: str,
    service: "RetrievalService",
    config: AppConfig,
    model: str,
    *,
    allowed_tools: set[str] | None = None,
    max_steps: int = 12,
    system: str = AGENT_SYSTEM,
) -> AgentResult:
    """Drive the vendored agent loop over our retrieval tool suite."""
    from knowledge_index.benchmark.gateway_adapter import GatewayAdapter
    from knowledge_index.benchmark.agent_harness.agent_loop import run_agent

    suite = ToolSuite(service, principal, allowed_tools=allowed_tools)
    system = _incorporate_server_instructions(system, suite.instructions)
    if allowed_tools is not None:
        # The server instructions describe the FULL tool surface; on an ablated
        # config the agent must not chase tools that don't exist in this session.
        names = ", ".join(sorted(allowed_tools))
        system += (
            f"\n\nIMPORTANT: only these tools are available in this session: {names}. "
            f"Ignore any guidance above that refers to other tools."
        )
    adapter = GatewayAdapter(config, model)
    # The forcing guard only applies where the tool exists: an ablated config that
    # was never given find_related_documents must not have it pinned.
    can_traverse = any(
        s["function"]["name"] == "find_related_documents" for s in suite.specs()
    )
    result = run_agent(
        adapter,
        system,
        query,
        suite,
        tools=suite.specs(),
        max_turns=max_steps,
        prepare_step=traverse_once if can_traverse else None,
    )
    answer = ""
    for message in reversed(result["messages"]):
        if message.get("role") == "assistant" and message.get("content"):
            answer = message["content"]
            break
    messages = result["messages"]
    if not answer.strip():
        # The loop hit max_steps while still tool-calling: force one final,
        # tool-free answer from what was gathered instead of scoring an empty
        # string — "ran out of steps" and "wrong" are different failures.
        final = gateway.complete(
            config,
            model,
            [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "Stop searching. Give your best final answer NOW based only on "
                        "what you already retrieved, citing the document it came from."
                    ),
                },
            ],
            usage_sink=adapter.usage,
        )
        answer = final.get("content") or ""
        messages = [*messages, final]
    return AgentResult(
        "agentic", answer, suite.retrieved_paths, suite.calls, result["turn_count"],
        trajectory=suite.trace, messages=messages, usage=adapter.usage,
    )
