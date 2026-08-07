# LegalMemory demo

A document browser and a chat, side by side, in front of a LegalMemory
appliance. The tree on the left is the firm's estate as folders. The chat on the
right answers questions about it and cites the documents it used. Clicking a
citation reveals that document in the tree and opens it in the middle.

Everything on screen is resolved against one identity. Change it and the tree,
the answers and the previews all change together, which is the point: the
permission compiler is not a filter applied to results, it runs before ranking.

## Running it

With the rest of the stack, from the repository root:

```bash
cp .env.demo.example .env.demo          # fill in the two model routes
docker compose -f docker-compose.demo.yml --env-file .env.demo up -d --build
open http://localhost:3200
```

Five containers: Postgres, OpenSearch, LiteLLM, the appliance, and this. See the
header of `docker-compose.demo.yml` for what is left out and what each omission
costs. Indexing new documents additionally needs Docling: add `--profile ingest`.

Against an appliance you already have running:

```bash
cp .env.example .env.local              # point LEGALMEMORY_API_URL at it
npm install && npm run dev
```

That appliance must have `KI_SECURITY__MCP_ALLOW_TRUSTED_HEADER=true`, because
it is how the demo authenticates. It is a development setting — anything that can
reach the port can claim any identity — so keep the port on localhost.

## How it is put together

```
browser ──► demo server ───────────────► appliance
            /api/tree            GET  /api/tree/*        folder listings
            /api/chat            POST /mcp               the model's tools
            /api/preview         POST /mcp               download_document
            /api/document-text   POST /mcp               get_document
```

The browser never talks to the appliance. It does not know the appliance's URL,
never holds the identity header, and cannot reach `/mcp`. That header is a bearer
credential in every way that matters, and a credential in a client bundle is a
credential you have published.

**The tree** (`src/components/file-tree.tsx`) is built for an appliance holding
fifty thousand documents, which rules out fetching the ledger and building a tree
from it. Folder structure is computed in SQL on the appliance
(`src/knowledge_index/web/tree.py`) as an aggregate on the next path segment
under a prefix; the client holds only the rows currently visible and renders them
virtualized. Folders arrive whole per level, files a page at a time.

**Revealing a citation** is one call. `/api/tree/locate` returns the connector,
the ancestor folders and the file's ordinal within its own folder, so the client
opens exactly the folders on the path and jumps to the page holding it — rather
than walking the tree a level per request.

**The chat** (`src/app/api/chat/route.ts`) runs the agent loop server-side with
the appliance's MCP tools, restricted to the five that read documents. Their
schemas are declared in `src/lib/mcp.ts` rather than discovered, which keeps tool
calls well-formed on a small model and lets the interface render a card per tool
by name (`src/components/tool-cards.tsx`): ranked hits with the passage that
matched, the document graph with the evidence behind each relation, matters with
their place in the practice-area ontology.

**Preview** (`src/app/api/preview/route.ts`) streams the original bytes through
the capability `download_document` issues — short-lived, identity-bound,
re-checked against the ACL snapshot on every read, recorded in the access ledger.
The demo has no other route to a document. PDFs go to the browser's viewer, Word
is laid out from its OOXML, spreadsheets keep their sheets, `.eml` is parsed and
rendered as the message it is. Formats a browser cannot open fall back to the
text the appliance extracted at insertion — labelled as such, with the original a
click away, because a preview that silently substitutes extracted text for a
signed PDF is worse than no preview.

**On a phone** the same three panels become tabs. They are not a second layout:
the panels are laid over each other and all but one is hidden in CSS
(`src/app/globals.css`, one media query, matched by `src/lib/use-compact.ts` for
the few decisions styling cannot make). Nothing is rebuilt when the breakpoint is
crossed, which is the point — a phone turned sideways would otherwise remount the
chat and throw the conversation away. Selecting a document brings its tab
forward, closing it goes back where the document was opened from, and a Word file
is reflowed to the width of the screen rather than laid out at the width of a
page.

## Configuration

| Variable | Default | |
|---|---|---|
| `LEGALMEMORY_API_URL` | `http://127.0.0.1:8010` | Where the appliance is |
| `LEGALMEMORY_MCP_URL` | `<API_URL>/mcp` | Override only if mounted elsewhere |
| `LEGALMEMORY_PRINCIPALS` | `role:admin` | The identity everything resolves against |
| `DEMO_MODEL` | `gemini-3.6-flash` | The chat model, by the name the gateway serves it under |
| `DEMO_MODEL_BASE_URL` | `http://127.0.0.1:4000/v1` | The gateway |
| `DEMO_MODEL_API_KEY` | — | The gateway's master key, not a provider key |

The chat model is routed through LiteLLM so the demo holds no provider key and
its spend lands in the same cost centre as the appliance's own. To change model,
change `DEMO_MODEL` and the matching `KI_DEMO_*` route in `.env.demo`.

## What it will not do

The assistant answers questions about the indexed documents and declines
everything else — general legal advice, drafting, world knowledge. That is a
system-prompt boundary, not a guarantee; it is a demo, not counsel.
