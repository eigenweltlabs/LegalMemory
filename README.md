# LegalMemory

Open-source, on-prem knowledge index for law firms. LegalMemory builds a
continuously synced **shadow index** over the firm's document estate — without
changing a single source file — and exposes it to AI tools through an **MCP
server**, with the firm's permissions enforced on every query.

Documents are not just embedded and retrieved by similarity. An insertion
pipeline first converts every document to structured data, assigns it to a
matter, links related documents (draft→final chains, annexes, referenced
contracts), extracts typed metadata from final versions, and captures
anonymized decision rationale from correspondence and redlines. Retrieval runs
inside a permission-compiled candidate set — identity → project grants →
mirrored source ACLs → search scope, deny wins, unknown fails closed.

**Try it:** [legalmemory.eigenweltlabs.com/demo](https://legalmemory.eigenweltlabs.com/demo)
— a document browser and a chat over a live index. Ask a question, and the
document the answer came from opens beside it. The same application is in
[`demo/`](demo/) and runs against your own index with five containers.

**Documentation:** the `docs/` folder is a [Starlight](https://starlight.astro.build/)
site — run `npm install && npm run dev` inside `docs/`, or read the pages
directly under `docs/src/content/docs/`. Deployments set `KI_DOCS_URL` so the
admin UI links to the hosted docs.

## Highlights

- **Source connectors** — SharePoint Online, OneDrive, Google Drive, Clio and
  local folders, plus a plugin contract for customer DMS exports. All
  read-only, bring-your-own-client OAuth, permissions mirrored (including
  group expansion for ethical walls).
- **Durable sync** with tombstones, checkpoints, ACL snapshots, deletion
  confirmation across scans, and provider change events (Azure Event Hubs,
  Google Pub/Sub) with scheduled reconciliation.
- **Seven-stage resumable pipeline**: conversion with German OCR and a
  raw-OOXML tracked-changes overlay, agentic matter classification, agentic
  version chains and typed relations, metadata extraction against a pluggable
  ontology, anonymized decision records, and hybrid indexing. Failing stages
  retry with backoff and quarantine; nothing degrades silently.
- **Permission compiler** applied before any ranking — project and document
  grants intersected with mirrored source ACLs, deny wins, verified under
  concurrent load.
- **Retrieval built for legal work**: four RRF-fused legs (lexical, dense,
  exact identifiers, decision records), version-status decay instead of age
  decay, and collapse to the one binding version of each document.
- **Identity-bound MCP tools** (search, matters, traversal, decisions,
  billing, entity resolution) behind OAuth 2.1 with audience binding, an
  append-only access ledger, and an admin console covering connectors,
  pipeline, ontology, data, access, identity, models, costs, external clients,
  activity and backup.
- **Structured legal knowledge** kept distinct from document metadata:
  LEDES/UTBMS billing (invoices, line items, timekeepers) and typed entity
  identifiers, with MCP tools to query them.
- **Full-appliance backup** — every store, encrypted, read back and
  re-verified before a run reports success, GFS retention, and a staged
  restore that refuses to run under the wrong credential key.
- **Retrieval benchmark** with frozen gold labels and a naive-dense baseline
  gate, so retrieval changes are regression-tested.

## Why it is built this way

Each retrieval decision here is a choice among published alternatives. The short version
with sources; the full evidence base, including where the literature disagrees, is in
[Design evidence](docs/src/content/docs/concepts/evidence.md).

**Filters and permissions are applied before ranking, never after.** Filtering a global
nearest-neighbour set afterwards degrades as the filter gets selective — the effect a
per-matter or per-client scope always produces:

- Gollapudi et al., [*Filtered-DiskANN*](https://doi.org/10.1145/3543507.3583552), WWW
  2023 — post-hoc filtering baselines "fail to achieve any meaningful accuracy, and have
  almost a 1000x lower QPS for the low specificity labels".
- Patel et al., [*ACORN*](https://doi.org/10.1145/3654923), SIGMOD 2024 — pre-filtering
  "always achieves perfect recall"; post-filtering degrades toward a full scan when the
  predicate is clustered.
- Li et al., [*Attribute Filtering in ANN Search*](https://doi.org/10.1145/3769763),
  SIGMOD 2026 — across 10 algorithms, applying the predicate before distance computation
  "consistently handles 0.1% selectivity across all datasets".
- Chronis et al., [*Filtered Vector Search*](https://doi.org/10.14778/3750601.3750700),
  VLDB 2025 — "A vector search execution method tuned for unfiltered queries will fail to
  achieve high recall when filters are added."

**Authorization is part of the query, not a post-filter.** Büttcher & Clarke,
[*A Security Model for Full-Text File System Search*](https://www.usenix.org/conference/fast-05/security-model-full-text-file-system-search-multi-user-environments),
USENIX FAST '05, shows that ranking over the whole corpus and removing forbidden files
afterwards lets a user infer the content of files they cannot read. Retrieval scope is
also a confidentiality boundary: embeddings leak their inputs (Song & Raghunathan,
[CCS 2020](https://doi.org/10.1145/3372297.3417270) — inversion recovers 50–70% of input
words) and RAG datastores are extractable through the generation interface (Zeng et al.,
[Findings of ACL 2024](https://aclanthology.org/2024.findings-acl.267/)).

**Retrieval is hybrid and fused, not dense-only.** In
[BEIR](https://datasets-benchmarks-proceedings.neurips.cc/paper/2021/hash/65b9eea6e1cc6bb9f0cd2a47751a186f-Abstract-round2.html)
(NeurIPS 2021) every dense retriever tested scored *below* BM25 averaged over 18
out-of-domain datasets, while BM25 plus a reranker led at +11% nDCG@10 — and a firm's own
corpus is by definition out of domain. Legs are combined with reciprocal rank fusion
(Cormack, Clarke & Büttcher, [SIGIR 2009](https://doi.org/10.1145/1571941.1572114)); an
exact-identifier leg exists because fixed-length dense vectors have bounded capacity for
exact terms (Luan et al., [TACL 2021](https://aclanthology.org/2021.tacl-1.20/)).
Reranking follows the multi-stage cascade (Wang, Lin & Metzler,
[SIGIR 2011](https://doi.org/10.1145/2009916.2009934); Sun et al.,
[EMNLP 2023](https://aclanthology.org/2023.emnlp-main.923/)) and is off by default, for
latency rather than doubt.

**Types come from a controlled vocabulary, and the ontology is pluggable.** Free-text
labelling fails because people do not choose the same word: two people pick the same term
for the same object with probability below 0.20 (Furnas et al.,
[CACM 1987](https://doi.org/10.1145/32206.32212)), and controlled vocabularies recover
results that keyword search alone misses (Gross & Taylor,
[C&RL 2005](https://doi.org/10.5860/crl.66.3.212)). The shipped taxonomy aligns with
[SALI LMSS](https://github.com/sali-legal/LMSS) and carries stable IRIs, but the ontology
is data, not code — any OWL/SKOS-shaped taxonomy can replace it, and it maps cleanly to
[LKIF-Core](https://ceur-ws.org/Vol-321/),
[LegalRuleML](https://doi.org/10.1145/2514601.2514603),
[ELI](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52012XG1026(01)) and
[CUAD](https://www.atticusprojectai.org/cuad).

**Answers carry citations, and retrieval changes are gated by a benchmark.** Public
models asked verifiable questions about federal cases hallucinate 58–88% of the time
(Dahl et al., [Journal of Legal Analysis 2024](https://doi.org/10.1093/jla/laae003)), and
a preregistered study of retrieval-augmented legal research systems measures 17–33%
(Magesh et al.,
[Journal of Empirical Legal Studies 2025](https://doi.org/10.1111/jels.12413)) —
retrieval augmentation reduces the problem without removing it. Hence provenance to a
specific document version on every result, and a frozen-gold benchmark gate rather than
asserted accuracy (cf. [LegalBench-RAG](https://arxiv.org/abs/2408.10343), which scores
the retrieval step directly).

## Quick start

```bash
cp .env.example .env    # fill in the required values — see the docs Quick start
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose up -d --build
bash scripts/bootstrap-hatchet.sh
docker compose exec app ki add-source /path/to/documents --name "First estate"
docker compose exec app ki sync
```

Open `http://127.0.0.1:8000` (admin console) and `http://127.0.0.1:8888`
(pipeline runs). The docs' *Quick start* page covers the required environment
and the first-run configuration in detail; cloud sources connect from the
console after registering a provider app (see the docs' connector guides).

## Repository layout

| Path | Purpose |
|---|---|
| `docs/` | The documentation site (Astro Starlight) |
| `src/knowledge_index/` | The Python package: ontology, sync engine, pipeline, retrieval, MCP server, web API |
| `src/knowledge_index/connectors/` | The connector layer: sources, OAuth runtime, ACL mirroring, provider events |
| `ui/` | The admin console (React + Vite; builds into `src/knowledge_index/web/static/`) |
| `deploy/` | Service configuration (Keycloak realm, LiteLLM gateway, Postgres) |
| `migrations/` | Alembic database migrations |
| `tests/` | The test suite (runs against real services; see `.github/workflows/README.md`) |
| `examples/` | The plugin-connector reference exporter |
| `external/` | Pin manifest for third-party repos studied but not vendored |

## Built on

LegalMemory is assembled from other people's work. Named here in full, with
licences, because several of these are load-bearing rather than incidental.

**Code that ships inside this repository**

| Project | Licence | What we took |
|---|---|---|
| [Airweave](https://github.com/airweave-ai/airweave) | MIT | Thirteen connectors — SharePoint Online, OneDrive, Teams, Outlook Mail, Outlook Calendar, OneNote, Google Drive, Google Docs, Gmail, Dropbox, Box, Notion, Confluence — with their entity schemas, and the shared connector framework they run on: the source base class and `@source` decorator, the retry and HTTP helpers, the entity base and field types, the cursor types, and the Purview sensitivity-label filter. Derived, not merely inspired: in most of these files the majority of lines are Airweave's, with our access-control mirroring, scoped sync, delta handling and credential host pinning layered on top. The Slack and Clio connectors are our own work. Per-file list and the MIT notice: `src/knowledge_index/connectors/NOTICE`. |

**The data model**

| Standard | Licence | How we use it |
|---|---|---|
| [SALI LMSS](https://www.sali.org/) — Legal Matter Standard Specification | MIT | The interoperability reference for our taxonomies; carried as an optional `sali_iri` annotation on practice area, matter kind, doc type and party roles. |
| LEDES 1998B/XML + UTBMS | open standards | The shape of the billing tables: invoices, line items, timekeepers, task/activity/expense codes. |
| [CUAD](https://www.atticusprojectai.org/cuad) | CC BY 4.0 | The clause-type vocabulary maps all 41 public benchmark labels, keeping public evaluation data usable. |

**Services we run alongside** — [PostgreSQL](https://www.postgresql.org/) +
[pgvector](https://github.com/pgvector/pgvector) (system of record),
[OpenSearch](https://opensearch.org/) (Apache-2.0; chosen over Elasticsearch on licence),
[Docling Serve](https://github.com/docling-project/docling) (MIT; conversion and German
OCR), [LiteLLM](https://github.com/BerriAI/litellm) (MIT core; model gateway),
[Hatchet](https://github.com/hatchet-dev/hatchet) (MIT; orchestration),
[Keycloak](https://www.keycloak.org/) and
[oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) (identity),
[FastMCP](https://github.com/jlowin/fastmcp) (Apache-2.0; the MCP server).

**Design we borrowed without taking code** — [Onyx](https://github.com/onyx-dot-app/onyx)
(MIT outside `ee/`): our `SyncSource` contract adopts its permissive connector
model — full crawl, incremental batches, cheap identity observations, opaque
cursors, per-item failures. Its enterprise permission-sync code is out of
bounds and was not read.

**Evaluation corpora** — CUAD, MAUD and
ContractNLI (CC BY 4.0, attributed per fixture); the EDRM Enron slice; Open
Legal Data dumps (ODbL) and German official works under §5 UrhG; LEXam and
BenGER for German regression sets.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see
[SECURITY.md](SECURITY.md).

Licensed under the [GNU Affero General Public License v3.0](LICENSE). Private
and internal use are unrestricted; the copyleft obligations attach when you
distribute a copy or offer a modified version to users over a network. Code in
this repository derived from MIT-licensed projects keeps its own notices — see
[NOTICE](NOTICE). Upstream components we studied but do not ship remain separate
works under the licences and pinned revisions recorded in `external/README.md`.

A commercial licence is available for organisations that cannot take on the
AGPL's terms. Contact Eigenwelt Labs.
