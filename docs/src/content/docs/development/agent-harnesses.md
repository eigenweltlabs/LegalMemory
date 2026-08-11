---
title: Putting LegalMemory in an agent harness
description: How to give an existing agent harness the appliance's MCP tools instead of a filesystem — the bridge, the tool contract, the prompt rules that change answers, and the budgets a corpus-wide question needs.
---

Most agent harnesses hand a model a **filesystem**: `glob`, `grep`, `read` over
a directory of documents. LegalMemory replaces that with an **index** — the
same read tools the product's own assistant uses, over a corpus the agent never
has to hold. This page is what we learned wiring the appliance into a
third-party harness: what to build, what to write in the system prompt, and
what to budget.

## The bridge

A working reference implementation is in this repository at
`integrations/agent-harness/legalmemory_mcp.py` — the JSON-RPC client, the
provider schema sanitizer, the executor wrapper and the prompt section. What
follows explains what it does and why, so you can port it rather than copy it.

It expects two things of whatever harness hosts it, which are one-line patches
in most: retry transient provider errors (429/503) inside the agent loop rather
than failing the run, and tolerate a `None` text part when a reasoning model
returns tool calls and no prose — both are certain over a long concurrent run.

The MCP endpoint is stateless streamable-HTTP with JSON responses, so a bridge
is a single POST per call — no session handshake, no SDK required.

```python
req = urllib.request.Request(
    f"{base_url}/mcp/",
    data=json.dumps({"jsonrpc": "2.0", "id": n, "method": method,
                     "params": params}).encode(),
    headers={"content-type": "application/json",
             "accept": "application/json, text/event-stream",
             "authorization": f"Bearer {token}"},
)
```

Two details that cost an afternoon if you miss them:

- The transport may frame a reply as a **single SSE event** even in JSON mode.
  Strip a leading `data:` before parsing.
- `tools/list` is the source of truth for **schemas and descriptions**. Fetch
  them at run start rather than transcribing them into your harness: the
  descriptions carry the pagination contract and the per-tool guidance, and
  they change with the appliance, not with your copy of it.

Identity comes from the bearer token (see
[External access](/product/external-access/)). The `x-ki-principals` trusted
header exists for a proxy that has already authenticated the caller and is off
by default — convenient on a developer's machine, never on a network.

### Pass the whole surface through

The temptation is to register a handful of obvious read tools and leave the
rest out — fewer tokens per turn, less for the model to wander into. Do not.
Curating the list is a decision about what the agent is *able* to do, taken by
the part of the system that knows least about the corpus.

A subset that keeps only search and read leaves no way to resolve a practice
area to its ontology node, so a question one filtered call answers becomes a
walk through the estate, matter by matter, until the agent runs out of turns.
Nothing in the transcript says a tool was missing. The agent simply does the
job the long way and returns a partial answer, and the run looks like a model
that was not thorough rather than a harness that removed the shortcut.

The server decides what it offers; the bridge passes it through and reads the
names from `tools/list` at run start.

### Schema dialects

Provider function-calling schemas are narrower than JSON Schema. Gemini
rejects `additionalProperties` outright and has no null type, so Pydantic's
`anyOf: [{type: T}, {type: "null"}]` for an optional argument must be folded
into `nullable: true`. Sanitize server schemas into the dialect your provider
accepts rather than hand-writing them; the appliance still validates every
call against its full schema, so nothing is lost but strictness the model
never saw.

## The tool contract

### Lists carry metadata, items carry evidence

A **listing row** — a search hit, a related-document row, a graph edge, a
matter — carries what you need to choose: `title`, `doc_type_label`,
`doc_date`, `matter_ref`, `parties` with their roles, the document's own
`identifiers`, `version_status`, the matched `excerpt`, `source_paths`, plus
the ids to act on. Where a set stands behind a row, the row carries its
**count** (`visible_versions`, `citation_count`, `document_ids`).

The **citation record** — project, document version, source objects, content
hashes — comes from the item-level tools: `get_document`,
`download_document`, `billing_rollup`.

This is worth knowing before you build a citation pipeline on top. An earlier
release embedded a citation per visible version into every matter row; a
100-row page was 2.9 MB, larger than most models' context window, and it
failed whole conversations. Rows are now ~460 bytes and search hits ~1.2 KB.
If your harness needs a citation for a document, it has to read the document —
which is what you want anyway, because a claim about a document you never
opened is the failure mode the citation was supposed to prevent.

### Search the estate, then search inside the document

Two searches, two scopes, and an agent that confuses them wastes its context:

- `search_semantic` ranks **the estate** and tells you which document to open.
- `search_in_document` ranks **one document's own passages** against a
  question and returns the few that answer it. This is the first call to make
  on a document you have identified — most questions about an agreement turn
  on one or two of its pages, and reading up to them costs the whole prefix.
- `get_document` reads a document through, a page of chunks at a time. This is
  the thorough read, and it is what two jobs require: enumerating everything
  of some kind in a document, and establishing that something is **not** in it.

Both document tools number by the same units, so a `search_in_document` hit
reports the `get_document_page` that holds it and the pages either side.
Reading around a hit is one call, and the page numbers address exactly the text
the passage came from as long as `chunks_per_page` matches.

### Page; never truncate

Every list-shaped tool returns `{results, page}`, and `page.has_more` is
exact. A full page is a sample, not an inventory. `get_document` is the same
contract in different units: a page is a **prefix**, and while `has_more` is
true there is text the agent has not seen — so no claim that something is
absent can be made from it. The reply says so in the text itself, not only in
the `page` block, because a field beside the prose gets skimmed past and the
prose reads complete.

Do not put a size cap in your bridge. It is tempting — one oversized result
can end a turn — but a clamp cuts a JSON payload mid-structure, turning a
complete answer into an unparseable one and, worse, into a *plausible* partial
one. If a result is too large, the call was too broad: lower the `limit`, add
a filter, or page. Say so in the tool-failure message you hand back to the
model and it will do exactly that.

### Return files as bytes, not as links

`download_document` answers with an MCP `resource` block carrying the original
binary, and with a URL. A bridge that keeps only the text blocks throws the
bytes away and leaves the agent holding a link to an appliance it cannot reach
from its sandbox — it then reports, correctly from where it stands, that the
document could not be retrieved. Write the resource into the agent's
**workspace**, and say in the tool result where it landed. The workspace is
the sandbox's directory, not your bridge process's working directory; defaulting
to `os.getcwd()` writes the file somewhere the agent cannot see and produces
exactly the same failure with an extra file on your disk.

## Prompt rules that change answers

Adapted from the product assistant's own prompt. Each of these exists because
a run failed without it.

**Work in order: fix the scope, search it exhaustively, read, then conclude.**
Most wrong answers skip a step rather than search badly, and the two most often
skipped are the middle ones — fixing a set from a listing without searching
inside it, and searching a great deal while reading almost nothing. Excerpts
are the passage that matched; the term being asked about is usually a paragraph
away.

**Distinguish what happened from what was prepared for.** A firm's files are
full of strategy memos and contingency plans for events that never occurred.
Before asserting an event, read the document that *constitutes* it — the
agency's letter, the filed complaint, the executed agreement — and date the
event from that document. Without this rule, agents report events that were
only ever drafted for, and date them from the memo.

**Precision counts as much as recall.** Including a near miss is as wrong as
missing a match. Verify each candidate's qualifying fact from a document you
read, and put the ones that did not qualify in their own labelled section.

**Enumerate before answering a question about a set.** "Which matters", "how
many", "have we ever" are questions about the estate, not about the first
matters that matched. Narrow with filters rather than walking the estate, then
page that set until `has_more` is false. Four correct matters out of eight is a
wrong answer, and nothing in the result tells you eight existed.

**Answer a superlative in two steps.** Assemble every qualifying matter, then
compare the deciding attribute across all of them. Agents reliably nominate the
first strong candidate they read, and the deciding dates are often days apart —
the more complete-looking story is frequently not the more recent one.

**Name documents by their path.** Take `source_paths` verbatim from the result
row rather than writing a prose title. Two documents in a matter often share a
title, and the path is what distinguishes them.

**Answer the question that was asked, then qualify.** A term of art means what
the market means by it. An answer that says "no" and then lists four examples
of the thing has let a caveat invert its own headline.

## Budgets

A corpus-wide question is not a chat turn, and the two limits that end it are
usually set for chat turns.

- **Steps.** A cross-matter sweep legitimately spends dozens of tool calls. A
  low double-digit cap ends runs mid-gather with no answer at all, and even a
  generous one truncates a multi-matter enumeration. Size the bound for the
  heaviest legitimate task — it exists to stop a looping model, not a working
  one.
- **Wall clock.** A request timeout is a step cap wearing a different hat.
- **Retries.** Over a long concurrent run, provider 429s and 503s are certain.
  Retry transient failures with backoff inside the agent loop; without it, an
  infrastructure blip is recorded as an agent failure and pollutes your
  evaluation.
- **Context.** Watch total prompt tokens per turn, not per call. An agent that
  reads whole documents when it needed one clause accumulates a transcript it
  then resends every turn, so the cost of a careless read is paid again on
  every turn after it. This is what `search_in_document` is for.

## What to measure

Run a handful of questions before running hundreds. The three defects that cost
us the most were all visible in the first single-question probe: a schema the
provider rejected, a model id that did not exist, and one tool call that
returned more than the context window could hold.

Then, per run, keep: turn count, the tool-call mix, tokens in and out, wall
clock, and whether the run finished cleanly or hit a bound. The mix is the most
diagnostic of these — a run that never called `find_related_documents` answered
from search alone, a run whose `search_semantic` calls all carry the same
arguments is stuck rather than thorough, and a run that called `get_document`
where `search_in_document` would have done is one you will see again in the
context numbers.
