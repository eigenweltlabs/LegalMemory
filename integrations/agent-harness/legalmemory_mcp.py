"""Give an agent harness the appliance's tools instead of a filesystem.

A reference bridge, written for the Harvey LAB harness and kept here because
nothing in it is specific to that harness: a single-POST JSON-RPC client for
the stateless /mcp mount, a schema sanitizer for providers whose
function-calling dialect is narrower than JSON Schema, an executor that routes
the appliance's tools and passes everything else through, and the working
guidance that measurably changed answers on 250 graded firm-knowledge tasks.

See docs/development/agent-harnesses.md for the surrounding advice — budgets,
what to measure, and the two upstream patches this expects of the harness it
plugs into (retry transient provider errors inside the agent loop, and tolerate
a None text part when a reasoning model returns tool calls only).

Enable by setting LEGALMEMORY_MCP_URL. Identity travels in the
x-ki-principals trusted header, which the appliance accepts only when
KI_SECURITY__MCP_ALLOW_TRUSTED_HEADER is on — a development setting for a
caller the deployment already authenticated, never an open network. In
production, present a bearer token instead.
"""

from __future__ import annotations

import json
import os
import urllib.request

# Every tool the appliance registers is exposed. Curating the list here was a
# mistake worth recording: a five-tool subset left the agent unable to resolve a
# practice area to its ontology node, so "every matter in the antitrust practice"
# — one filtered call — became a 174-turn crawl through 266 matters. The server
# decides what it offers; the harness passes it through.
READ_TOOLS: list[str] = []  # filled from tools/list at run start


# The keyword dialect Gemini function declarations accept. Everything else —
# additionalProperties, $-bookkeeping, const — is dropped; the appliance still
# validates calls against its full schema, so nothing is lost but strictness
# the model never saw anyway.
_SCHEMA_KEYS = {
    "type", "format", "title", "description", "nullable", "default", "items",
    "minItems", "maxItems", "enum", "properties", "required", "minimum",
    "maximum", "minLength", "maxLength", "pattern", "anyOf",
}


def _gemini_safe(schema):
    """Reduce a JSON Schema to the dialect Gemini function declarations accept.

    Pydantic emits optionals as ``anyOf: [{type: T}, {type: "null"}]``; Gemini
    has no null type, so the null variant folds into ``nullable: true`` on the
    surviving branch. ``default: null`` is dropped for the same reason.
    """
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k not in _SCHEMA_KEYS or (k == "default" and v is None):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _gemini_safe(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _gemini_safe(v)
        elif k == "anyOf" and isinstance(v, list):
            variants = [
                x for x in v
                if not (isinstance(x, dict) and x.get("type") == "null")
            ]
            nullable = len(variants) != len(v)
            variants = [_gemini_safe(x) for x in variants]
            if len(variants) == 1:
                for mk, mv in variants[0].items():
                    out.setdefault(mk, mv)
            elif variants:
                out["anyOf"] = variants
            if nullable:
                out["nullable"] = True
        else:
            out[k] = v
    return out


class McpBridge:
    """Single-POST JSON-RPC client for the appliance's stateless /mcp mount."""

    def __init__(self, url: str | None = None, principals: str | None = None):
        base = url or os.environ["LEGALMEMORY_MCP_URL"]
        self.url = base.rstrip("/") + "/"
        self.principals = principals or os.environ.get(
            "LEGALMEMORY_PRINCIPALS", "role:admin"
        )
        self.call_counts: dict[str, int] = {}
        self._id = 0

    def _post(self, method: str, params: dict) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "x-ki-principals": self.principals,
            },
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode()
        # The streamable-HTTP transport may frame the reply as one SSE event.
        if raw.lstrip().startswith(("event:", "data:")):
            raw = next(
                line[5:].strip()
                for line in raw.splitlines()
                if line.startswith("data:")
            )
        resp = json.loads(raw)
        if "error" in resp:
            raise RuntimeError(f"MCP {method} error: {resp['error']}")
        return resp["result"]

    def tool_definitions(self) -> list[dict]:
        """Every tool the appliance registers, schemas and descriptions as served."""
        listed = self._post("tools/list", {})["tools"]
        READ_TOOLS[:] = [t["name"] for t in listed]
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": _gemini_safe(
                    t.get("inputSchema", {"type": "object", "properties": {}})
                ),
            }
            for t in listed
        ]

    def call(self, name: str, arguments: dict) -> str:
        self.call_counts[name] = self.call_counts.get(name, 0) + 1
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        texts = [
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        ]
        payload = result.get("structuredContent")
        out = (
            json.dumps(payload, ensure_ascii=False)
            if payload is not None
            else "\n".join(texts)
        )
        if result.get("isError"):
            return f"Tool error: {out or 'unspecified'}"
        return out


class McpToolExecutor:
    """Routes the MCP read tools to the appliance; everything else passes through.

    Wraps (rather than subclasses) ToolExecutor so the sandbox lifecycle,
    path resolution and file-tool behaviour stay exactly stock.
    """

    def __init__(self, inner, bridge: McpBridge):
        self._inner = inner
        self._bridge = bridge

    def execute(self, tool_name: str, arguments) -> str:
        if tool_name in READ_TOOLS:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError as exc:
                    return f"Tool error: arguments were not valid JSON ({exc})"
            try:
                return self._bridge.call(tool_name, arguments or {})
            except Exception as exc:  # surface, never crash the loop
                return f"Tool error: {type(exc).__name__}: {exc}"
        return self._inner.execute(tool_name, arguments)

    def get_metrics(self) -> dict:
        metrics = self._inner.get_metrics()
        metrics["mcp_calls"] = dict(self._bridge.call_counts)
        metrics["mcp_calls_total"] = sum(self._bridge.call_counts.values())
        return metrics

    def __getattr__(self, name):
        return getattr(self._inner, name)


# Adapted from the LegalMemory demo's system prompt: the tool inventory, the
# pagination contract, and the working order it was measured to need. UI- and
# chat-specific rules (citation markup, decline policy) are replaced by the
# deliverable conventions of this harness.
MCP_PROMPT_SECTION = """

## The firm's knowledge base

The task documents are NOT on your filesystem — `documents/` is empty. The
firm's entire estate is indexed in a knowledge appliance you query through
these tools:

The appliance exposes its full tool surface and each tool carries its own
description — read them. The ones that carry most questions:

- search_semantic — hybrid semantic and lexical search; the entry point.
- search_filter — exact metadata with no query. Filters include matter_id,
  doc_type, party, date range and **practice_area**, which matches a whole
  practice with subtree semantics. This is how a question about "every matter
  in the X practice" is answered — one filtered call, not a walk through the
  estate.
- list_taxonomies / ontology_search / ontology_roots / ontology_children —
  resolve a practice area, document type or service to the ontology node id
  that the filters take. Do this before filtering by practice area.
- get_document — read one document's text, paginated.
- find_related_documents / traverse — stored relations with the evidence that
  established them.
- list_matters — the matter inventory; takes practice_area too.
- resolve_entity, search_decisions, billing_rollup, list_invoices,
  preview_search_scope — entities, drafting rationale, matter billing, and
  what this identity can see.

Every list-shaped tool returns `{results, page}`, not a bare list.
`page.has_more: true` means more matched than you were shown; the next page
is the same call with `offset: page.next_offset`. A full page is a sample,
not an inventory — so before you say "all", "every", "none", "only", or give
a count, either page until `has_more` is false or say which part of the set
you looked at.

## How to work the knowledge base

Follow this order. Most wrong answers come from skipping a step, not from a
bad search.

1. **Search broadly once.** One search_semantic with no filters, to find out
   where the answer lives.
2. **Narrow to the matter.** The hits carry matter_id. Once you know which
   matter the question is about, search again with that matter_id. Use
   search_filter with the matter_id and no query to see everything in it.
3. **Read what you found.** Do not answer from search excerpts. An excerpt
   is the passage that matched; the terms you are being asked about are
   usually a paragraph away from it. get_document the candidates.
4. **Traverse before you conclude.** Call find_related_documents on the
   documents you are relying on. A term sheet has a definitive agreement; a
   draft has a final; a brief has its exhibits. Those links are stored with
   evidence and a search will not recover them.

Step 4 matters most when you are about to say something is *absent*. "There
is no such document" is a strong claim, and traversing the relations of what
you did find is how you check it rather than assume it.

Do not repeat a search with the same arguments — if it returned little,
change the query or the filter, or move on. Never guess at the contents of a
document you did not read.

**Distinguish what happened from what was prepared for.** A firm's files
contain strategy memos, drafts and contingency plans for events that never
occurred. Before asserting that an event occurred — a second request was
issued, a suit was filed, a deal closed — read the document that
*constitutes* the event (the agency's letter, the filed complaint, the
executed agreement), not a memo that anticipates or plans for it. Date an
event from the constituting document.

**Precision counts as much as recall.** When a task asks which matters or
documents qualify, including a non-qualifying item is as wrong as missing a
qualifying one. For each candidate, verify the qualifying fact from a
document you read; put near-misses (prepared-but-not-issued,
considered-but-not-done) in a separate clearly-labelled section rather than
in the qualifying set.

**A question about a practice area is one filtered call.** Resolve the area
with list_taxonomies (or ontology_search), then pass its node id as
`practice_area` to search_filter or list_matters — the filter matches the
whole subtree, so a parent area covers its children. Never enumerate a
practice by reading matters one at a time: it is slower by two orders of
magnitude, it misses every matter you did not reach before you stopped, and
the filter already knows the answer.

**Answer a question about a set by narrowing, not by walking — and then be
complete.** "Which matters", "how many", "pull every" and "have we ever" are
questions about the whole estate, and the answer has to cover it. What
changes is the method, never the standard: filter the estate down to the
candidate set (practice area, document type, party, date range, identifier)
in a call or two, page that set until `has_more` is false, and verify every
candidate in it by reading a document. Reading the estate matter by matter
is the wrong method — it is slow and it silently stops early. But a partial
answer is still wrong: if the task says every, give every. If a filter
leaves more candidates than you expected, that is the work, not a reason to
sample.

**Answer a superlative in two steps.** For the latest, the earliest, the
largest, the first: assemble every matter that qualifies at all, then
compare the deciding attribute across all of them and name the winner with
that attribute stated. Do not nominate the first strong candidate you read
— the deciding dates are often days apart, and the most complete-looking
story is frequently not the most recent one.

**Fix the definition before you enumerate, and hold it.** Many tasks turn
on a term of art — covenant-lite, maintenance covenant, second request,
closed. Decide what the term means, in the sense the market uses it, say so
in one line, and apply that same line to every candidate. An answer that
counts a borderline feature as qualifying in one paragraph and disqualifying
in another is wrong whichever line was right.

**Answer the question that was asked, then qualify it.** Lead with the
direct answer in the questioner's own vocabulary. If the facts support a
yes under the ordinary meaning of the words, say yes, give the count and
name the matters, and then state the caveat. Never let a qualification
invert the headline: an answer that says "no" and then lists four examples
of the thing is a wrong answer.

When your investigation is complete, write your answer to `response.md` with
the `write` tool. Name every document you rely on by its path in the firm's
filing system — take it verbatim from `source_paths` on the result row, e.g.
`1003-00001/FTC/second-request-strategy-memo.docx`. A prose title is not an
identification: two documents in one matter often share a title, and the
path is what distinguishes them. Where a task asks you to pull or include
documents, list the paths. State counts and "all/none" claims only after
paging to `has_more: false`.
"""
