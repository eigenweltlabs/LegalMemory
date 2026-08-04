---
title: Ontology
description: "The pluggable ontology artifact: file format, facets, scoping semantics, re-typing behavior, and the doc-type health metrics."
---

LegalMemory classifies every document against one pluggable ontology
*artifact*. The artifact is data, never code: a deployment plugs one in, the
console activates *facets* of it, and the scoped view (artifact minus
disabled subtrees, restricted to active facets) is the sole vocabulary source
for the extraction agent, retrieval filters, the MCP taxonomy tools, and the
console. There is exactly one answer to "what document types exist here".

## The artifact model

### File format

An artifact is a single JSON document, optionally gzip-compressed (the loader
detects the gzip magic bytes and accepts either). Top-level fields:

| Field | Meaning |
| --- | --- |
| `name` | Artifact name, e.g. `lmss`. |
| `version` | Artifact revision string, e.g. `2026-07-27.2`. |
| `source_url` | Where the source ontology file came from (informational). |
| `source_sha256` | SHA-256 of the exact source file the artifact was built from. |
| `facets` | Map of facet name to a list of root node ids. |
| `nodes` | Map of node id to `{l: label, p: [parent ids], d: definition, s: [synonyms]}`. `p`, `d`, `s` are optional. |

Node ids are the IRI tails of the source ontology (e.g.
`RDMmVnDBUmOnVx8i4ZpOt2G`), so they stay stable across artifact rebuilds even
when a label is reworded. Nodes may have multiple parents; the structure is a
DAG, not a strict tree. Child lists are derived at parse time and sorted by
label; the parsed artifact is immutable in memory.

### What ships by default

The package ships one artifact, `lmss.json.gz` (about 2.7 MB compressed),
built from the SALI LMSS legal ontology:

- `name: lmss`, `version: 2026-07-27.2`, 18,322 nodes.
- Four facets: `doc_type` (roots *Document Types*, *Knowledge Type*, and
  *Written Asynchronous Communication*; the last one gives correspondence
  forms such as email and letter a home in the type facet), `area_of_law`
  (root *Area of Law*), `service` (root *Service*), and `clause` (root
  *Contractual Clause*).
- The `doc_type` facet covers roughly 1,500 of the 18,322 nodes.

### Regenerating an artifact

`scripts/build_ontology_artifact.py` builds an artifact from an OWL file. The
pipeline never parses OWL at runtime; only this script does, once:

```
python scripts/build_ontology_artifact.py LMSS.owl \
    --name lmss --version 2026-07-27 \
    --source-url https://raw.githubusercontent.com/sali-legal/LMSS/main/LMSS.owl \
    --out src/knowledge_index/ontology_data/lmss.json.gz
```

The script extracts `rdfs:label`, `rdfs:subClassOf`, `skos:definition`, and
`skos:altLabel` from every `owl:Class`, records the source file's SHA-256, and
locates facet roots by their labels in the source. All four facets are always
included in the artifact even though a deployment may activate fewer;
activating a facet later is configuration, not a rebuild. Output is
minified JSON, gzipped with a fixed mtime when the target ends in `.gz`, so
rebuilds from identical input are byte-identical.

### Discovery, uploads, and overrides

Artifacts are discovered by name (the filename before `.json`/`.json.gz`) from
two places, in order:

1. The packaged data directory inside the installed package (`lmss` by
   default).
2. The uploads directory `<artifact_dir>/ontologies` (default
   `.ki/artifacts/ontologies`). An uploaded file with the same name as a
   packaged artifact overrides it.

`POST /api/ontology/artifacts` (admin only) uploads a new artifact as a
multipart file. The filename must end in `.json` or `.json.gz`; the payload is
fully parsed before it is written to disk, so an invalid artifact is rejected
with `422` and never stored. Uploading does not activate the artifact;
activation is a scope change (`PUT /api/ontology/scope` with `artifact` set,
or setting `ontology.artifact` in configuration).

Loading is cached on `(file path, file mtime)`, and the scoped view on
`(file path, file mtime, active facets, disabled nodes)`, so replacing the
file or changing the scope is picked up without a restart, and repeated
resolution per task is cheap.

### The scope fingerprint

Every scoped view has a stable 16-hex-character fingerprint: the truncated
SHA-256 of `name | version | source_sha256 | active facets | sorted disabled
nodes`. The fingerprint is recorded on every document the extraction stage
types (`Document.ontology_fingerprint`, also in the document's `provenance`),
so an artifact or scope change knows exactly which documents were typed under
which view.

## Facets

| Facet | Consumer | How it is consumed |
| --- | --- | --- |
| `doc_type` | The extract-metadata stage (document typing) | Agentic walk: the agent gets `ontology_search`, `ontology_roots`, `ontology_children`, and `ontology_node` tools over the scoped facet and must submit a node id it has actually seen in a tool result; the result is additionally validated against the visible set. |
| `area_of_law` | The classify-matter stage (practice area) | Shallow facet: a compact indented id/label menu (depth ≤ 2) is embedded directly in the prompt; the returned node is validated by resolution against the facet. Only offered when the facet is active. |
| `service` | The classify-matter stage (matter kind) | Deep facet walked with tools and judged by definitions, under the same visited-id discipline as document typing. Only offered when the facet is active. |
| `clause` | The extract-metadata stage (notable clauses) | A `clause_search` tool over the facet; each clause's type node must come from a search result and be visible. Only offered when the facet is active. |

Every consumer resolves exactly one facet by name; the document-typing agent
never sees Area of Law roots and vice versa. Resolution happens at task
execution time: each pipeline task constructs its runner with a freshly read
configuration, so a mid-run artifact or scope change applies to every
not-yet-processed document. The console and cross-facet search use a separate
browse view that combines all active facets; pipeline producers never do.

Any node, interior or leaf, is a valid classification. "Stopping high" is the
honest catch-all; depth pressure (below) is the health signal. Navigation is
deterministic: roots, children, and node detail are pure lookups, and the
lexical search has a stable ranking (exact label > label prefix > label
substring > synonym substring > definition substring, ties alphabetical) with
no model calls.

## Scope semantics

The scope is stored in configuration as three values: the active artifact
name, the list of active facets, and a sorted list of disabled node ids.
`PUT /api/ontology/scope` (admin only) accepts any subset of
`{artifact, active_facets, disabled_nodes}`, validates that the resulting
artifact resolves before saving, and persists the new configuration.

Visibility is computed as: reachable from an active facet root without
crossing a disabled node. Disabling a node therefore hides its entire
subtree (except nodes also reachable through a visible parent elsewhere in
the DAG). Effects of a hidden branch:

- **Extraction**: the agent's navigation tools simply never show hidden
  nodes, and a submitted node outside the visible set is rejected by the
  validator. A stored id that later falls out of scope resolves to its
  nearest visible ancestor when displayed.
- **Retrieval filters**: documents and chunks store the *ancestor closure*
  of their type node (`doc_type_ancestors`), and the `doc_type` search filter
  matches against that closure. Filtering by an interior node ("Agreements")
  matches every document typed at or below it. Hidden nodes are not offered
  by ontology search, so they stop being discoverable filter values.
- **MCP taxonomy tools**: `list_taxonomies`, `ontology_search`,
  `ontology_roots`, `ontology_children`, and `ontology_node` all resolve the
  scoped `doc_type` facet from current configuration per call, so a scope
  change is visible to MCP clients immediately.

### When a change takes effect, and re-typing

Saving a scope change does two things:

1. **Future documents**: every pipeline task re-reads configuration when it
   starts, so the next task to run resolves the new scope. A task already
   in flight finishes under the scope it resolved at start; nothing is
   interrupted.
2. **Selective re-typing**: the server re-queues the extract-metadata stage
   (and everything downstream) for exactly two groups: documents whose type
   node is no longer visible, and documents honestly left untyped under a
   *different* fingerprint (a richer artifact may finally have a home for
   them). Documents whose node is still visible keep their result. Only
   settled documents (extract-metadata done or skipped) are re-queued; the
   response reports `requeued_documents` and, when any were re-queued,
   best-effort launches an insertion run (`run` in the response). An
   unreachable orchestrator does not fail the scope change; the rows stay
   pending for the next trigger.

## The Ontology page in the console

The page header shows the active artifact (name, version), the visible node
count, and the combined fingerprint. Each active facet renders as a
lazily-expanded tree; a search box covers labels, synonyms, and definitions
across all active facets. Administrators toggle subtrees off with the eye
icon and apply the change, which issues the `PUT /api/ontology/scope` call
described above.

The tree editor deliberately shows more than the pipeline sees:
`GET /api/ontology/children` returns children from the *unscoped* artifact
with two flags per child, `disabled` (explicitly toggled off) and `hidden`
(invisible because an ancestor is disabled), so disabled branches remain
visible and can be re-enabled.

## Vocabulary health: depth pressure

`GET /api/health/doc-types` measures where the ontology fails to fit, live
during a run. Documents that settle at shallow nodes (depth ≤ 2 below a
facet root) signal that the ontology lacks a fitting subtree there. The
response:

| Field | Meaning |
| --- | --- |
| `fingerprint` | The `doc_type` scope fingerprint the numbers were computed under. |
| `branches` | Per top-level branch (keyed by root label): `total` typed documents whose ancestor closure contains that root, `shallow` of those at depth ≤ 2, and `share` = shallow/total (0 when the branch is empty). A document under multiple roots counts in each. |
| `shallow_nodes` | The 20 most-used nodes at depth ≤ 2, each with `id`, `label`, `depth`, and document `count`, the exact nodes needing extension. |
| `untyped_documents` | Documents where the extraction agent found no fitting node at all (`doc_type` is null). |
| `stale_typed_documents` | Documents typed at a node that is not visible under the *current* scope; counted, not re-attributed, until re-typing runs. |
| `alerts` | See below. |

Two alert kinds are emitted:

- `depth_pressure`: a branch has at least 50 typed documents and more than
  25% of them sit at depth ≤ 2.
- `untyped_share`: at least 20 documents have been judged in total and more
  than 10% of them are untyped. This is the strongest signal to extend the
  artifact: those documents found no home in this ontology at all.

Depth is the length of the display path from the facet root, so a root itself
is depth 0. A healthy corpus classifies most documents into leaf or near-leaf
nodes; sustained pressure on one branch is the cue to plug a richer artifact
and let selective re-typing pick up exactly the affected documents.

## Configuration

| Key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `ontology.artifact` | `KI_ONTOLOGY__ARTIFACT` | `lmss` | Name of the active artifact (filename before `.json`/`.json.gz`). |
| `ontology.active_facets` | `KI_ONTOLOGY__ACTIVE_FACETS` | `["doc_type", "area_of_law", "service", "clause"]` | Facets the deployment activates. Consumers of inactive facets skip that work. |
| `ontology.disabled_nodes` | `KI_ONTOLOGY__DISABLED_NODES` | `[]` | Node ids whose subtrees are hidden from every consumer. |
| `artifact_dir` | `KI_ARTIFACT_DIR` | `.ki/artifacts` | Uploaded artifacts live in `<artifact_dir>/ontologies`. |

## Endpoints

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /api/ontology` | Signed in | Artifact identity, available artifacts, active facets, disabled nodes, combined fingerprint, and per-facet `{fingerprint, visible_nodes, roots}`. |
| `GET /api/ontology/children?node_id=` | Signed in | Tree-editor children from the unscoped artifact, with `disabled`/`hidden` flags. |
| `GET /api/ontology/search?q=` | Signed in | Lexical search across all active facets (up to 20 results, each with its path). |
| `PUT /api/ontology/scope` | Admin | Change artifact, facets, or node toggles; re-queues affected documents. |
| `POST /api/ontology/artifacts` | Admin | Upload a `.json`/`.json.gz` artifact; parsed and validated before storage. |
| `GET /api/health/doc-types` | Signed in | Depth pressure, shallow nodes, untyped and stale counts, alerts. |

## Related

- [Data model](/concepts/data-model/): how ontology labels appear on
  documents and chunks.
- [Insertion pipeline](/product/pipeline/): the stages that consume each
  facet.
