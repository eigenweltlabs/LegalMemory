"""Give an agent harness the appliance's tools instead of a filesystem.

A reference bridge, written against a third-party research harness and kept
here because nothing in it is specific to that harness: a single-POST JSON-RPC
client for the stateless /mcp mount, a schema sanitizer for providers whose
function-calling dialect is narrower than JSON Schema, an executor that routes
the appliance's tools and passes everything else through, and the working
guidance an agent needs to use an index instead of a filesystem.

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

import base64
import json
import os
import urllib.request

# Every tool the appliance registers is exposed. Curating the list here was a
# mistake worth recording: a subset that kept only the obvious read tools left
# the agent unable to resolve a practice area to its ontology node, so a
# question one filtered call answers became a walk through the whole estate,
# matter by matter, until it ran out of turns. A missing tool does not announce
# itself — the agent just does the job the long way and reports a partial
# answer. The server decides what it offers; the harness passes it through.
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

    def __init__(
        self,
        url: str | None = None,
        principals: str | None = None,
        workspace_dir: str | os.PathLike[str] | None = None,
    ):
        base = url or os.environ["LEGALMEMORY_MCP_URL"]
        self.url = base.rstrip("/") + "/"
        self.principals = principals or os.environ.get(
            "LEGALMEMORY_PRINCIPALS", "role:admin"
        )
        self.call_counts: dict[str, int] = {}
        self._id = 0
        # Where a returned file is written. The agent reads and writes files in its
        # own sandbox, so "the same place" is the SANDBOX workspace — pass it in.
        # Falling back to this process's cwd is the wrong default dressed as a safe
        # one: the file is written, the call reports success, and the agent cannot
        # see it. It then hunts for a document that is on the host, and the run ends
        # in a search of the filesystem rather than of the estate.
        self.workspace_dir = str(
            workspace_dir or os.environ.get("LEGALMEMORY_WORKSPACE") or os.getcwd()
        )

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

    def _save_resources(self, blocks: list) -> list[str]:
        """Write any binary `resource` blocks into the workspace; return their names."""
        saved: list[str] = []
        for block in blocks:
            if block.get("type") != "resource":
                continue
            resource = block.get("resource") or block
            data = resource.get("blob") or resource.get("data")
            if not data:
                continue
            name = (
                resource.get("name")
                or (resource.get("uri") or "").rsplit("/", 1)[-1]
                or "document.bin"
            )
            # basename only: a resource must never be able to name a path outside
            # the workspace it is being written into.
            name = os.path.basename(name) or "document.bin"
            try:
                target = os.path.join(self.workspace_dir, name)
                with open(target, "wb") as handle:
                    handle.write(base64.b64decode(data))
                saved.append(name)
            except Exception:  # noqa: BLE001 - a failed save must not kill the call
                continue
        return saved

    def call(self, name: str, arguments: dict) -> str:
        self.call_counts[name] = self.call_counts.get(name, 0) + 1
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        blocks = result.get("content", [])
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]

        # A tool that returns a FILE returns it as an MCP `resource` block, not as
        # text. Keeping only the text blocks threw the bytes away and left the agent
        # holding the accompanying download URL -- which points at the appliance on
        # localhost and is unreachable from the sandbox the agent runs in. It would
        # then report, correctly from where it stood, that the document could not be
        # retrieved. Write the bytes into the workspace and say where they landed.
        saved = self._save_resources(blocks)

        payload = result.get("structuredContent")
        out = (
            json.dumps(payload, ensure_ascii=False)
            if payload is not None
            else "\n".join(texts)
        )
        if result.get("isError"):
            return f"Tool error: {out or 'unspecified'}"
        # Appended after the branch, not inside it: a tool that returns a file also
        # returns structuredContent, so a note added to `texts` alone is discarded on
        # exactly the calls that need it -- the file lands in the workspace and the
        # agent is never told it is there.
        if saved:
            out = f"{out}\n\nSaved to the workspace: {', '.join(saved)}. Read these "
            out += "from disk; the download URL is not reachable from here."
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
# pagination contract, and the working order. UI- and chat-specific rules
# (citation markup, decline policy) are replaced by the deliverable conventions
# of this harness.
MCP_PROMPT_SECTION = """

## The firm's knowledge base

The documents are NOT on your filesystem — the working directory holds none of
them. The firm's entire estate is indexed in a knowledge appliance you query
through these tools:

The appliance exposes its full tool surface and each tool carries its own
description — read them. The ones that carry most questions:

- search_semantic — hybrid semantic and lexical search; the entry point.
- search_filter — exact metadata with no query. Filters include matter_id,
  doc_type, party, date range and **practice_area**, which matches a whole
  practice with subtree semantics. This is how a question about "every matter
  in the X practice" is answered — one filtered call, not a walk through the
  estate.
- Both search tools also take **practice_group**, **firm_person** and
  **lifecycle**, so a search can be scoped to one practice's matters, one
  lawyer's, or one state, instead of searching the estate and sifting. They are
  the same filters list_matters takes and they cover the same matters.
- list_taxonomies — the facets and their ids. Practice-area ids live under
  `practice_areas` here; this is where a practice_area filter value comes from.
- ontology_search / ontology_roots / ontology_children / ontology_node —
  navigate the DOCUMENT-TYPE ontology. These are a different facet from the
  practice areas: an id from here is not a practice_area, and passing one as
  practice_area matches nothing.
- search_in_document — ranks ONE document's own passages against a question and
  returns the few that answer it, each with the get_document page it sits on and
  its neighbouring pages. The way into a document you have identified.
- get_document — reads that document through, a page of chunks at a time
  ({page, pages, first_chunk, last_chunk, has_more, next_page}). The thorough
  read: what an enumeration needs, and the only basis for saying a document does
  NOT contain something, since a page is a prefix.
- find_related_documents / traverse — stored relations with the evidence that
  established them.
- list_matter_documents — EVERY document in one matter, complete, not paged.
  The folder view. Call it before deciding a matter lacks something.
- list_matters — the matter inventory. Each row states what the matter IS
  (instrument, principal_document, summary), whether it happened
  (lifecycle: executed | closed | terminated | dormant | in_progress), and
  who ran it (firm_team, practice_group, practice_groups). Filters:
  practice_area, lifecycle, practice_group, firm_person.
- list_firm_people — this firm's own lawyers: title, practice group, roles,
  matter count. The directory behind the practice_group and firm_person
  filters; use it to learn what a group is called and who is in it.
- resolve_entity, search_decisions, billing_rollup, list_invoices,
  preview_search_scope — outside entities (clients, counterparties), drafting
  rationale, matter billing, and what this identity can see.

Every list-shaped tool returns `{results, page}`, not a bare list.
`page.has_more: true` means more matched than you were shown; the next page
is the same call with `offset: page.next_offset`. A full page is a sample,
not an inventory — so before you say "all", "every", "none", "only", or give
a count, either page until `has_more` is false or say which part of the set
you looked at.

## How to work the knowledge base

Four steps, in order. Most wrong answers come from skipping one, not from a
bad search — and the two most often skipped are the middle ones: fixing a set
from a listing without ever searching inside it, and searching a great deal
while reading almost nothing.

1. **Establish the scope.** Turn the question into a set of matters with the
   filters — practice_group, practice_area, matter_kind, firm_person,
   lifecycle, party, doc_type, dates — and page until has_more is false. For a
   question about one matter that set is one matter; for "which of our X" it
   is a book. Everything after this is bounded by what you fix here, so say in
   your answer what the scope was. A listing tells you what is IN scope. It
   does not tell you what QUALIFIES.

2. **Search inside the scope, exhaustively.** Now search — search_semantic
   carrying the same filters, or matter_id for a single matter — and search
   several ways. A firm names things in its own words, and the word you tried
   is rarely the only one used; a concept can be titled differently in two
   matters and differently again inside one agreement. One query is a probe,
   not a survey. list_matter_documents shows a matter whole;
   find_related_documents reaches what ranking will not.

3. **Read the documents.** An excerpt marks where a match happened, not where
   the answer is: a provision sits four fifths of the way into a long agreement
   as readily as at the front. Read every candidate you mean to include — and
   every candidate you mean to exclude, because an exclusion is an assertion
   too. What "read" costs depends on what you are asking of the document, and
   the document tools say which of them fits; match the depth to the claim you
   intend to make.

4. **Conclude, and show the ground.** State the scope you searched, the test
   you applied, and the set that passed it.

Step 3 matters most when you are about to say something is *absent*. "There is
no such document" and "this matter does not qualify" are strong claims, and
reading to the end of what you found is how you check them rather than assume.

Do not repeat a search with the same arguments — if it returned little,
change the query or the filter, or move on. Never guess at the contents of a
document you did not read.

**Status answers whether, not what.** version_status, only_final and lifecycle
say whether a document is operative and whether a matter happened. They say
nothing about what kind of thing it is. An unsigned agreement is weak evidence
that a deal closed and strong evidence of what the parties were agreeing to —
it still shows the structure, the security, the terms. So drop unexecuted
versions when the question turns on occurrence, and read them like anything
else when the question asks what a matter or a deal IS. Counting only the
executed ones for a question about kind undercounts, and the undercount reads
as a confident answer.

**Distinguish what happened from what was prepared for.** A firm's files
contain strategy memos, drafts and contingency plans for events that never
occurred. Before asserting that an event occurred — a second request was
issued, a suit was filed, a deal closed — read the document that
*constitutes* the event (the agency's letter, the filed complaint, the
executed agreement), not a memo that anticipates or plans for it. Date an
event from the constituting document.

**Decide from the document, and say what decided it.** A candidate you could
not settle is unfinished work, not a finding: go back and read. If you still
cannot settle it, include it and say exactly what is unresolved — an answer
honest about one doubtful member is worth more than one that quietly drops a
matter that belonged.

**A question about a practice area is one filtered call.** Resolve the area
with list_taxonomies — its `practice_areas` list — then pass that node id as
`practice_area` to search_filter or list_matters — the filter matches the
whole subtree, so a parent area covers its children. Never enumerate a
practice by reading matters one at a time: it is slower by two orders of
magnitude, it misses every matter you did not reach before you stopped, and
the filter already knows the answer.

**A practice GROUP is not a practice area.** The group is how the firm
organises itself — "Banking & Finance", "Capital Markets", "Funds & Asset
Management", "Healthcare & Life Sciences" — and it is the group of the
partner who owns the matter. The area is which body of law applies. They
differ: a corporate formation run by the funds partners is a Funds & Asset
Management matter. When the question names a practice the way the firm
would ("our Banking & Finance matters", "the funds team's"), scope with
`list_matters(practice_group=...)`, not practice_area. The filter matches
every group with someone on the matter, so a jointly staffed deal is
returned to each of them — and each row then says which it was: `group_match`
is 'owner' when that group runs the matter and 'supporting' when it was merely
staffed onto one another group runs. Mind the difference. "Which matters does
the tax group have" means the ones it owns; a tax partner sitting on someone
else's IPO is work the group has touched, and counting it inflates the answer.
Say which set you are reporting. Check the group's name and who is in it with
list_firm_people first.

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

**Fix the definition before you enumerate, and hold it.** Many questions
turn on a term of art — covenant-lite, maintenance covenant, second request,
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
filing system — take it verbatim from `source_paths` on the result row, which
looks like `<matter-ref>/<folder>/<filename>`. A prose title is not an
identification: two documents in one matter often share a title, and the
path is what distinguishes them. Where the question asks you to pull or
include documents, list the paths. State counts and "all/none" claims only
after paging to `has_more: false`.

`response.md` MUST end with a section headed `## Sources`, listing one full
`source_paths` value per line, grouped by matter, verbatim, no prose titles and
no abbreviation.

List every document WITH A RELATION TO THE QUESTION — not the subset you
happened to open. If a document is in a matter you put forward and it relates to
what the question asks about, it belongs in the list, whether you read it,
skimmed it or only saw it in the folder. The rows handed you the paths already; the listing
tools give you the whole folder in one call. A reader of your answer wants to
know where the answer lives, and "here is what I personally opened" is not that.

Two failure modes, and the second is the common one. Listing a document you have
no basis for is a false citation. Omitting one that plainly relates to the question
because you did not open it spoils an answer that was already researched
correctly — the matters were right, the paths were on the rows in front of you,
and the closing checklists and executed agreements were dropped from the answer
for having gone unread.

Write this section LAST and do not shorten it when the answer runs long; it is
the first thing a reviewer checks and the cheapest thing to get exactly right.
Where you relied on a document for a specific assertion, say so in the body of
the answer next to the assertion — that is what supports a claim. This section
is the map of the ground, not the record of your reading.
"""
