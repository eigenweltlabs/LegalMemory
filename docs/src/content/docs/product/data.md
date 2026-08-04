---
title: Data
description: "The document explorer: the ACL-scoped graph projection, the paginated document ledger, the full document record, and original-file downloads."
---

The **Data** page is the document explorer: two views, graph and table, over
the same ACL-scoped estate, one shared filter toolbar, and a document drawer
that shows the complete stored record. Everything on the page is served by the
API under the caller's own principals; there is no separate "admin view" of
the same data, only a larger authorized set.

## Graph view

`GET /api/graph` compiles a permission-aware projection of the corpus and
returns `{nodes, edges, summary}`.

### Query parameters

| Parameter | Effect |
| --- | --- |
| `project_id` | Restrict to documents in one project. |
| `query` | Case-insensitive substring match on document *titles* only. |
| `doc_type` | Exact match on the document's stored ontology node id. |
| `matter_id` | Restrict to one matter. |
| `version_status` | Keep documents that have an authorized version with this status. |
| `language` | Exact match on document language. |
| `limit` | Maximum documents in the projection, capped at 10,000. `0` or omitted means the complete projection; when the limit cuts documents off, `summary.truncated` is `true` and `summary.total_documents` still reports the full count. |

### Node and edge kinds

Every node carries `id` (`kind:entity_id`), `entity_id`, `kind`, `label`, a
`size` used for rendering, and a `properties` object with the entity's stored
fields (for a document: project, matter, type, language, date, parties,
identifiers, version and chunk counts, provenance, timestamps; for a source
object: external id, path, container, MIME type, size, content hash, and
first/last seen).

| Node kind | What it is |
| --- | --- |
| `project` | Authorization boundary. |
| `matter` | Legal matter, with reference numbers, practice area, kind, status. |
| `document` | Logical work product. |
| `version` | One concrete, content-addressed document version. |
| `thread` | Reconstructed communication thread (included only when reached through a stored relation). |
| `source` | Connected DMS or filesystem source. |
| `source_object` | Exact source-system observation of a version (path, hash, size). |

Edges are of two classes. Structural edges are derived from the rows
themselves: `contains` (project→matter, matter→document, project→document
when the document has no matter, matter→thread, source→source_object),
`version_of` (document→version), and `observed_as` (version→source_object).
Stored relations (the typed edges the relate stage wrote, such as
`references`, `responds_to`, `supersedes`, `annex_of`, `belongs_to_thread`)
are included whenever both endpoints are visible, and are marked with
`stored: true` plus the relation's id, provenance, and creation time so they
remain distinguishable from derived structure.

`summary` reports node/edge totals, `documents` versus `total_documents`,
per-kind counts (`by_kind`, `by_edge_kind`), and the `truncated` flag.

### ACL scoping of the projection

The projection is filtered at every layer, not post-hoc:

- Documents come from the caller's visible-document set; versions are
  additionally filtered by the version-level access predicate.
- Source objects appear only when the caller may see them: admins see all;
  otherwise a matching `allow` grant with no matching `deny` is required, and
  ungranted objects are visible only on local-filesystem sources. A source
  node appears only if at least one of its objects is visible.
- Stored relations are included only when *both* endpoints are visible;
  threads enter the projection only via such relations.

The graph a member sees is genuinely smaller than an administrator's: nodes
are absent, not blurred.

### What the console renders

The console renders the projection with Cytoscape. Node color follows kind
and node diameter follows the projection's `size` field (projects largest,
source objects smallest). Three layouts are offered: *Matter clusters* (a
precomputed layout that places each matter's documents in a grid under the
matter, versions under their document, and source observations under each
version), *Grid*, and *Entity rings* (concentric by kind). The filter bar
hides whole node kinds, restricts to one edge kind, and finds a node by
title, path, or id; toggles control node/edge labels and fullscreen. Stored
relations draw heavier and more opaque than derived structure. A legend shows
visible versus total counts and a "Projection truncated" notice when the
server cut the document list. Selecting a node or edge opens an inspector
with all its properties, its connections (with direction and edge kind), and
the raw graph record as JSON; a document node offers "Open document record",
which opens the drawer without discarding the graph layout, zoom, or
selection.

## Table view

The table is served by `GET /api/documents` with `detailed=true`.

### Filters and pagination

| Parameter | Effect |
| --- | --- |
| `project_id` | Documents in one project. |
| `query` | Case-insensitive substring match on the title. |
| `doc_type` | Exact match on the stored type node id (row filter only; the facet counts ignore it, so the type breakdown always describes the otherwise-filtered set). |
| `matter_id` | Exact matter id. |
| `version_status` | Documents with an authorized version of this status (`draft`, `final`, `executed`, `unknown`). |
| `language` | Exact language match. |
| `limit` / `offset` | Page size (clamped to 1–5,000; default 500) and page start. |
| `detailed` | `true` returns the paginated envelope below; without it the endpoint returns a plain list for API clients. |

Only documents with at least one version the caller may read are listed at
all: the ledger joins through versions and applies the version-level access
predicate. The detailed envelope is:

- `items`: one row per document, carrying title, type, language, date, parties,
  identifiers, version and chunk counts, latest version's status/id/hash, and
  compact project and matter summaries.
- `pagination`: `total`, `offset`, `limit`, `returned`, `has_more`.
- `facets`: `doc_types` and `languages` as `{value, count}` lists over the
  authorized, filtered set, for populating filter dropdowns with real counts.

The console shows columns Document, Type, Project, Date, Versions, and
Status, plus a collapsible panel for the matter-id, version-status, and
language filters (page sizes 100–1,000). The command palette's matter lookup
uses `GET /api/matters?query=`, with matters matched by their own title, scoped
through the caller's visible documents; the returned `documents` count is
how many of the matter's documents the caller may read, and a matter whose
readable count is zero never appears.

### The search-scope ribbon

Pressing Enter in the search box runs `POST /api/search` with the current
filters. Besides `hits` (chunk-level results with document/version ids, type
id and resolved label, score, excerpt, source paths, and citations), the
response carries a `scope` block compiled from the caller's ACL *before*
ranking:

| Field | Meaning |
| --- | --- |
| `scope.fingerprint` | Stable fingerprint of the compiled access scope, the identity of the candidate set the query ran against. |
| `scope.projects` | Number of projects in the scope. |
| `scope.documents` | Number of documents in the candidate set. |
| `scope.filters` | Echo of the non-empty filters that constrained the search. |

The ribbon renders this as "N document(s) across P project(s) · fingerprint
…", the visible proof of what was searchable *for this caller* when the
results were produced. Note the semantic difference from the table: the
search `doc_type` filter matches the indexed ancestor closure (an interior
ontology node covers its whole subtree), while the table's `doc_type` filter
is an exact match on the stored node id.

## The document drawer

Opening a row (or a graph document node) loads
`GET /api/documents/{document_id}`, the complete authorized record. The
drawer's sections and the fields behind them:

| Section | Backing fields |
| --- | --- |
| Header and meta | `document.title`, `document.doc_type` with the resolved `doc_type_label`, `version.ordinal`/`status`, `document.language`, `document.doc_date`, version and related counts. |
| Extracted metadata | Type, language, date, `parties`, `identifiers`, and the ontology path: `doc_type_path` (root-to-leaf labels resolved from the live artifact) with `doc_type_ancestors` (the stored id closure) as fallback, plus `ontology_fingerprint`, the scope the document was typed under. A document typed under an artifact that has since been unplugged still shows the stored ids rather than an empty box. |
| How this was extracted | `extractions[]`, newest first: one row per extraction audit record with the `fields` it set, the `model`, the `prompt_version`, the `confidence`, and the timestamp. This is what a firm disputing a classification asks for first. |
| Notable clauses | `clauses[]` from the `notable_clauses` artifact stored for the version's content hash, the clause pass of the extract-metadata stage. |
| All identifiers | Document, project, matter, and version ids, the version's content hash, and `latest_final_version_id`, each copyable. |
| Document content | `content.text` from the parsed `structured_json` artifact, truncated with an expand control. |
| Source provenance | `sources[]`, the source objects behind the selected version: connection, path, and object id. |
| Version history | `versions[]`, ordered newest-ordinal first: id, ordinal, status, content hash, `status_evidence`, `redline_against`, provenance, and the source observations behind each version. Each entry is independently authorization-checked; a version with no source the caller may read is omitted entirely. |
| Related documents | `related`, documents connected through stored relations or a shared matter/thread, each entry naming the basis (`stored_relation` vs. derived context) and relation kind, and each independently ACL-checked. |
| Document access exceptions | `grants[]`, document-level allow/deny overrides, read-only here; they are managed on [Access control](/product/access-control/). No entries means project grants and mirrored source ACLs apply unmodified. |
| Raw record | The matter, document, version, content-metadata, and entire API payloads as expandable JSON. |

The endpoint returns `404` both for an unknown id and for a document none of
whose versions the caller is authorized to read; the two cases are
indistinguishable by design.

## Original-file downloads

Exact original bytes are exported through short-lived capability links. The
MCP `download_document` tool resolves an authorized document version to its
cached original blob (authorization is checked against the source object, not
merely the document), issues a capability token, and returns a URL of the
form `/api/downloads/{token}/{filename}` together with `expires_in_seconds`.

Properties of the link, served by `GET /api/downloads/{token}/{filename}`:

- The token is a 32-byte random URL-safe value; the store is process-local
  and in-memory, and entries expire after 300 seconds.
- The capability itself is the credential (no session is required to fetch
  it), but **every** fetch re-checks the ACL snapshot using the principals
  captured when the tool issued the link. A grant revoked after issuance
  invalidates an otherwise unexpired URL; the token is then revoked and the
  request fails with `404`.
- The filename in the URL must match the capability's filename exactly.
- The blob on disk must still exist with the recorded size, otherwise `410`
  and revocation. Responses stream with `Cache-Control: private, no-store`.

## Edge cases

| Situation | What you see |
| --- | --- |
| Document not yet indexed | The record loads (typing, versions, and provenance come from the database), but the chunk count is 0 and it does not appear in search results. |
| No parsed text for a version | `content` is null; the drawer states that no structured text artifact is available instead of showing an empty preview. |
| Document with no readable version | Absent from the table, the graph, and search; `GET /api/documents/{id}` returns `404`. |
| Version with no readable source observation | Omitted from the version history; the version count reflects only authorized versions. |
| Matter with no readable documents | Never returned by `GET /api/matters`; the `documents` count on a returned matter is the caller's readable count, not the matter's size. |
| Type node no longer in the active ontology scope | `doc_type_label`/`doc_type_path` resolve to nothing; the drawer falls back to the stored ancestor ids, and the [ontology health endpoint](/product/ontology/) counts the document as stale-typed until re-typing runs. |
| Untyped document | Listed as "Unclassified"; the extraction record still shows which scope judged it (`ontology_fingerprint`), so a richer artifact later re-types exactly these documents. |

## Related

- [Data model](/concepts/data-model/) describes the entities behind the projection:
  documents, versions, source objects, relations.
- [Insertion pipeline](/product/pipeline/): the stages that produce every
  field the drawer shows.
- [Access control](/product/access-control/): how the visible sets and
  document exceptions are computed.
