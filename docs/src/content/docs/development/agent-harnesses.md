---
title: Putting LegalMemory in an agent harness
description: How to give an existing agent harness the appliance's MCP tools instead of a filesystem — the bridge, the tool contract, the prompt rules that measurably change answers, and the budgets a corpus-wide question needs.
---

Most agent harnesses hand a model a **filesystem**: `glob`, `grep`, `read` over
a directory of documents. LegalMemory replaces that with an **index** — the
same five read tools the product's own assistant uses, over a corpus the agent
never has to hold. This page is what we learned wiring the appliance into a
third-party harness and grading the result against a public benchmark of 250
firm-knowledge tasks: what to build, what to write in the system prompt, and
what to budget.

Everything below is measured. Where a number appears, it came from a graded
run, not from taste.

## The bridge

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
by default — convenient for a local benchmark, never for a network.

### Restrict the surface deliberately

Registering every tool the appliance exposes is rarely right. For a research
or drafting agent, five read tools are the whole job:

| Tool | Use |
| --- | --- |
| `search_semantic` | hybrid semantic + lexical search; the entry point |
| `search_filter` | exact metadata, no query; enumerate a matter |
| `get_document` | read one document's text, paginated |
| `find_related_documents` | stored relations with the evidence behind them |
| `list_matters` | the matter inventory, for "what exists" questions |

Leave `download_document` out unless the agent genuinely needs original bytes,
and leave the ontology and billing tools out unless the task is about the
ontology or billing. Every extra tool is context the model pays for on every
turn and one more way for it to wander.

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

### Page; never truncate

Every list-shaped tool returns `{results, page}`, and `page.has_more` is
exact. A full page is a sample, not an inventory.

Do not put a size cap in your bridge. It is tempting — one oversized result
can end a turn — but a clamp cuts a JSON payload mid-structure, turning a
complete answer into an unparseable one and, worse, into a *plausible* partial
one. If a result is too large, the call was too broad: lower the `limit`, add
a filter, or page. Say so in the tool-failure message you hand back to the
model and it will do exactly that.

## Prompt rules that changed answers

Adapted from the product assistant's own prompt, then hardened against a
graded task set. Each of these was written because a run failed without it,
and each measurably moved the score.

**Work in order: search broadly, narrow to the matter, read, then traverse.**
Most wrong answers skip a step rather than search badly. Excerpts are the
passage that matched; the term being asked about is usually a paragraph away —
so read the document rather than answering from the hit.

**Distinguish what happened from what was prepared for.** A firm's files are
full of strategy memos and contingency plans for events that never occurred.
Before asserting an event, read the document that *constitutes* it — the
agency's letter, the filed complaint, the executed agreement — and date the
event from that document. Without this rule, agents reported a second request
that was only ever drafted for, and dated it from the memo.

**Precision counts as much as recall.** Including a near-miss is as wrong as
missing a match. Verify each candidate's qualifying fact from a document you
read, and put the near-misses in their own labelled section.
*Measured: adding this and the rule above took criteria pass rate from 51.5%
to 61.8% across ten tasks, and one enumeration task from 3/5 to 5/5.*

**Enumerate before answering a question about a set.** "Which matters", "how
many", "have we ever" are questions about the estate, not about the first
matters that matched. Sweep from several angles — practice area, instrument,
parties, the vocabulary a document would actually use — until a sweep stops
producing candidates you have not assessed. Four correct matters out of eight
is a wrong answer, and nothing in the result tells you eight existed.

**Answer a superlative in two steps.** Assemble every qualifying matter, then
compare the deciding attribute across all of them. Agents reliably nominate
the first strong candidate they read; on our task set the deciding dates were
**one day apart**, and the more complete-looking story was not the more recent
one.

**Name documents by their path.** `1038-00001/Correspondence/letter-ftc-meet-and-confer.docx`,
not "the meet-and-confer letter". Two documents in a matter often share a
title, and `source_paths` is on every row.

**Answer the question that was asked, then qualify.** A term of art means what
the market means by it. An answer that says "no" and then lists four examples
of the thing has let a caveat invert its own headline.

## Budgets

A corpus-wide question is not a chat turn, and the two limits that end it are
usually set for chat turns.

- **Steps.** A cross-matter sweep legitimately spends dozens of tool calls.
  A 12-step cap ended runs mid-gather with no answer at all; 60 still
  truncated eight-matter enumerations. Size the bound for the heaviest
  legitimate task — it exists to stop a looping model, not to stop a working
  one.
- **Wall clock.** A request timeout is a step cap wearing a different hat.
- **Retries.** Over a long concurrent run, provider 429s and 503s are
  certain. Retry transient failures with backoff inside the agent loop;
  without it, an infrastructure blip is recorded as an agent failure and
  pollutes your evaluation.
- **Context.** Watch total prompt tokens per turn, not per call. Agents that
  page deeply accumulate; if your provider bills cached input separately,
  route through a gateway so the number you read is the number you pay.

## What to measure

Run a handful of tasks before running hundreds. The three defects that cost us
the most were all visible in the first single-task probe: a schema the
provider rejected, a model id that did not exist, and one tool call that
returned more than the context window could hold.

Then, per task, keep: turn count, the tool-call mix, tokens in and out, wall
clock, and whether the run finished cleanly or hit a bound. The mix is the
most diagnostic of these — a run that never called `find_related_documents`
answered from search alone, and a run whose `search_semantic` calls all carry
the same arguments is stuck rather than thorough.
