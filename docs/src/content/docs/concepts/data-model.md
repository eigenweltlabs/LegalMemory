---
title: The data model
description: The three-layer data model — source observations, interpreted knowledge, pipeline state — and the design rules everything is built on.
---

The data model for the shadow index. Everything else — sync, pipeline, retrieval, MCP
tools, the UI — is defined against these entities. The model has three layers:

1. **Source layer** — immutable observations of the customer's systems. Never
   interpreted, never overwritten, only superseded. This is what sync writes.
2. **Knowledge layer** — interpreted entities (matters, documents, relations,
   decisions). This is what the pipeline writes and what retrieval reads.
3. **Pipeline layer** — per-object processing state, provenance, and errors. This is
   what makes the system resumable and debuggable at millions of documents.

Design rules that apply everywhere:

- **Provenance on every inference.** Any field produced by a model carries
  `extracted_by` (model id + prompt version), `confidence` (0–1), and `evidence`
  (source spans / object ids). Deterministic facts (hashes, mtimes, paths) carry none.
- **Nothing destructive.** Source objects are tombstoned, never deleted; knowledge
  entities are superseded by new versions, never mutated in place. Re-running a
  pipeline stage with a newer model creates a new extraction, it does not overwrite
  the audit trail.
- **Content-addressed where possible.** Binary content is identified by SHA-256, so
  the same file appearing in five places (mail attachment, DMS, file share, two
  drafts folders) is processed once and linked five times.
- **English ids, localized labels.** Every taxonomy id and enum value is a stable
  English snake_case string; localization (German display labels for a German firm)
  is a UI concern, never baked into the data model. Firms can extend taxonomies;
  core ids never change meaning. Free-text the model produces (titles, summaries,
  anonymized rationales) is kept in the *document's* language so firm knowledge
  reads naturally — only the controlled vocabulary is English.

---

## 1. Source layer

### Source
A configured connector instance (one SharePoint site, one iManage library, one SMB
share, one mailbox).

| Field | Notes |
|---|---|
| `id` | uuid |
| `kind` | `sharepoint \| imanage \| netdocuments \| smb \| local_fs \| imap \| ra_micro \| actaport \| ...` |
| `display_name` | e.g. "Fileserver K:\Mandate" |
| `config_ref` | pointer to connector config (secrets live in the secret store, not here) |
| `cursor` | opaque incremental-sync state (Graph delta token, USN, last-scan watermark) |
| `sync_policy` | full-scan interval, poll interval, webhook on/off |
| `status` | `active \| paused \| error` |

### SourceObject
One file/email/container **as seen in one source**. The unit of sync and of pipeline
processing. The same logical document in two systems is two SourceObjects (later
joined in the knowledge layer via content hash / near-dup detection).

| Field | Notes |
|---|---|
| `id` | uuid |
| `source_id` | FK Source |
| `external_id` | the source system's own stable id (Graph item id, iManage docnum+version, inode+path fallback) |
| `path` | human path within the source, verbatim (umlauts, garbage and all) |
| `name` | filename / mail subject |
| `container` | parent folder / mailbox folder / DMS workspace |
| `mime_type`, `size_bytes` | as reported |
| `content_hash` | SHA-256 of bytes, filled after first fetch |
| `source_version_label` | the DMS's own version string if it has one (iManage v3, SP version 12.0) |
| `mtime`, `ctime`, `author_hint` | source metadata, untrusted but useful signals |
| `acl` | list of `AccessGrant` (below), as readable from the source; `null` = source exposes no ACLs |
| `first_seen`, `last_seen`, `deleted_at` | tombstone via `deleted_at`, never row-delete |

### Blob & Artifact
`Blob` = unique content (`content_hash`, `size`, `mime_sniffed`, optional cached copy
subject to retention policy — the shadow index does **not** need to retain originals).
`Artifact` = derived output keyed by `(content_hash, producer, producer_version)`:
extracted text, structured JSON (layout, tables, tracked changes, comments), page
images, OCR confidence map, embeddings. Artifacts are immutable and reproducible;
cache-invalidation is "new producer_version, new artifact".

### AccessGrant
`{principal, principal_kind: user|group|source_role, access: allow|deny, raw}` —
`raw` preserves the source-native ACL entry so nothing is lost in translation.
Principals are mapped to firm identities (AD/Entra) in a separate `PrincipalMapping`
table maintained by the connector.

---

## 2. Knowledge layer

### Client
| Field | Notes |
|---|---|
| `id` | uuid |
| `name`, `aliases[]` | all name forms seen for one client entity |
| `kind` | `legal_entity \| natural_person` |
| `identifiers` | company-register number, tax id, DMS client code — whatever the firm has |
| provenance | confidence + evidence (clients are usually *imported* from the practice-management system and are then authoritative, `confidence = 1.0`) |

### Matter
The central aggregation (a firm's "matter" or "file"). Documents belong to matters;
retrieval is matter-aware.

| Field | Notes |
|---|---|
| `id` | uuid |
| `reference_numbers[]` | all matter reference numbers seen (firm's own, court's, opposing counsel's) |
| `title` | e.g. "Müller GmbH v. Schmidt AG — share purchase" |
| `client_ids[]` | FK Client |
| `practice_area` | taxonomy below |
| `matter_kind` | `transaction \| litigation \| advisory \| regulatory \| internal` |
| `parties[]` | `MatterParty { party_id, role }` — roles from the `PartyRole` taxonomy: `client, opposing_party, opposing_counsel, court, authority, notary, advisor, other` |
| `responsible[]` | lawyers/teams (principal refs) |
| `status` | `active \| closed \| unknown` |
| `time_range` | earliest/latest document dates |
| provenance | matters are *inferred* by the pipeline unless imported from practice-management; keep both, prefer imported |

### Party (natural or legal person)
Shared by matters (an opposing party in one matter may be the client in another —
conflict checks care about exactly this). `{id, name, aliases, kind, identifiers}`.

### Document (logical) and DocumentVersion
A **Document** is the logical work product ("the SPA for project Falke"); a
**DocumentVersion** is one concrete state of it. Version chains are first-class
because metadata extraction runs on final versions only, and rationale extraction
runs on the deltas between versions.

Document:
| Field | Notes |
|---|---|
| `id`, `matter_id` | |
| `doc_type` | taxonomy below |
| `title` | normalized ("Share Purchase Agreement", not "SPA_final_FINAL_v3(2)") |
| `language` | `de \| en \| mixed \| ...` |
| `doc_date` | the date *of the document* (signing date, letter date) — distinct from any file mtime |
| `parties[]` | parties to the document itself |
| `latest_final_version_id` | resolved pointer, null if no final identified |

DocumentVersion:
| Field | Notes |
|---|---|
| `id`, `document_id` | |
| `source_object_ids[]` | all places this exact content was seen |
| `content_hash` | FK Blob |
| `ordinal` | position in the chain (1 = earliest known) |
| `status` | `draft \| final \| executed \| unknown` — `executed` = signed scan/qualified signature |
| `status_evidence` | why we think so: filename signals, signature blocks, email context ("attached is the final version"), PDF-of-docx pairing |
| `redline_against` | version id this is a markup of, if it carries tracked changes |

### Relation
Typed edges between knowledge entities. One table, `{from, to, kind, confidence,
evidence}`:

| kind | meaning |
|---|---|
| `version_of` | DocumentVersion → Document (structural) |
| `annex_of` | annex/exhibit → main document |
| `references` | mentions/cites (contract → side letter, pleading → judgment) |
| `amends` | amendment agreement → amended contract |
| `supersedes` | replacement relationships |
| `responds_to` | email → email, pleading → pleading |
| `duplicate_of` / `near_duplicate_of` | exact (hash) / fuzzy (MinHash/simhash) |
| `belongs_to_thread` | email → CommunicationThread |
| `work_product_of` | final document → EvalRecord input set |

### CommunicationThread
Reconstructed email/message threads: `{id, matter_id, participants[], subject_norm,
time_range}`. Threads are the main substrate for decision-rationale extraction.

### DecisionRecord
The anonymized "why" — extracted from threads and redlines, stored decoupled from
client identity so it is usable as firm knowledge.

| Field | Notes |
|---|---|
| `id`, `matter_id` (internal only), `document_id`, `version_from`, `version_to` | |
| `locus` | clause/section reference (e.g. "§ 9 para. 2 limitation of liability"), in the document's language |
| `change_summary` | what changed between the versions |
| `rationale_category` | `legal_risk \| market_standard \| negotiation_concession \| regulatory_requirement \| drafting_error \| client_instruction \| tactical` |
| `rationale_text` | **anonymized** prose in the document's language: parties → roles ("the seller", "the client"), names/amounts normalized |
| `generalizable` | bool — `client_instruction` and matter-specific tactics default to `false` and are excluded from cross-matter retrieval |
| `source_evidence` | thread/email/version ids (ACL-protected; the anonymized text may be surfaced more broadly than its evidence, policy-controlled) |
| provenance | model, prompt version, confidence |

### EvalRecord (internal benchmark record)
Generated at insertion when a completed task is recognized: a final work product plus
the inputs that existed before it.

| Field | Notes |
|---|---|
| `id`, `matter_id`, `task_type` | taxonomy below |
| `instruction` | reconstructed task statement, in the document's language (e.g. "Draft a managing-director service agreement based on …") |
| `input_refs[]` | DocumentVersions available at task start |
| `reference_output_ref` | the human final version (gold answer) |
| `rubric[]` | `{criterion, description, weight, kind: binary\|scale_1_5}` — derived from what the final version actually does: clauses present, positions taken, formalities met |
| `holdout` | bool — excluded from the retrieval index to stay valid as a benchmark |
| provenance | |

---

## 3. Pipeline layer

### MatterAssignment

Matter classification is stored per `SourceObject`, not per content hash, because the
same bytes can appear in two folders with different path and ACL evidence. The row
records `source_object_id`, `matter_id`, confidence, evidence and producer version.
Exact duplicates still share conversion artifacts while retaining source context.

### ProcessingState
One row per `(source_object_id, stage)`. Stages (see architecture doc):
`fetch → convert → classify_matter → relate → extract_metadata → extract_decisions →
gen_evals → index`.

| Field | Notes |
|---|---|
| `status` | `pending \| running \| done \| failed \| quarantined \| skipped` |
| `attempts`, `next_retry_at` | exponential backoff, capped |
| `last_error` | class + message + truncated trace |
| `producer_version` | pipeline/prompt/model version that produced `done` — bumping it re-queues the stage |

`quarantined` is the poison-document terminal state: visible in the UI, counted, never
blocking the rest of the corpus. `skipped` is policy (e.g. >2 GB media file, excluded
path pattern).

### Extraction
Generic audit record for every model call that wrote knowledge-layer data:
`{id, target_entity, target_field(s), model, prompt_version, input_artifact_refs,
raw_output_ref, confidence, created_at}`.

### AuditEvent
Append-only access ledger for the client surface and MCP tools:
`{id, actor_principals, action, target_type, target_id, outcome, details, created_at}`.
Search text is not retained; query-bearing tools store only character count and a
SHA-256 fingerprint. Authorized, denied and failed MCP invocations are all recorded.
If the ledger cannot be written, an MCP tool fails closed rather than serving an
unlogged result.

---

## 4. Taxonomies (v0 — firm-extensible)

> **Note:** document-type, area-of-law, service and clause vocabularies are now
> supplied by the pluggable ontology artifact and scoped in the admin UI — see
> [Ontology in the product guide](/product/ontology/). The lists below document
> the built-in v0 baseline the default artifact extends.

Ids are stable English snake_case, mirrored exactly in `taxonomies.py`. A German firm
sees German labels in the UI; the stored ids never change.

### PracticeArea
`corporate_ma`, `commercial`, `labor`, `real_estate`, `litigation`, `ip_it`, `tax`,
`banking_finance`, `insolvency`, `public`, `criminal`, `family_inheritance`, `other`.

### DocType (grouped; leaf ids stable)
- **contract**: `purchase_agreement`, `share_purchase_agreement`, `lease_agreement`,
  `employment_agreement`, `managing_director_agreement`, `nda`,
  `articles_of_association`, `loan_agreement`, `license_agreement`,
  `data_processing_agreement`, `amendment_agreement`, `other_contract`
- **pleading**: `statement_of_claim`, `statement_of_defense`, `appeal_brief`,
  `other_pleading`, `motion`
- **court**: `judgment`, `court_order`, `court_directive`, `hearing_minutes`
- **correspondence**: `email`, `letter`, `secure_mailbox_message`, `client_memo`
- **internal**: `internal_note`, `legal_opinion`, `research_memo`,
  `due_diligence_report`, `checklist`, `note`
- **evidence**: `commercial_register_extract`, `land_register_extract`,
  `power_of_attorney`, `invoice`, `external_expert_report`, `other_annex`
- **administration**: `fee_agreement`, `engagement_agreement`, `deadline_note`,
  `other_admin`

### TaskType (for DecisionRecords and EvalRecords)
`contract_drafting`, `contract_review`, `negotiation`, `due_diligence`,
`legal_opinion`, `claim_drafting`, `defense_drafting`, `legal_research`,
`summarization`, `legal_translation`, `compliance_review`, `other`.

---

## 5. ACL propagation policy

- **Doc-level is law.** A DocumentVersion is retrievable by a user iff the user can
  access ≥1 underlying SourceObject (union of grants, deny wins within one source).
- **Matter metadata** (title, parties, existence) inherits the union of its documents'
  grants by default; firms with strict ethical walls can switch to
  `matter_restricted` mode where matter visibility requires explicit membership.
- **DecisionRecords/EvalRecords** are anonymized derivatives; default policy exposes
  the anonymized text firm-wide but the `source_evidence` only per doc-level ACL.
  Both are configurable (`decision_visibility: firmwide | acl | off`).
- Sources without readable ACLs (plain SMB without AD resolution) get a per-source
  default grant set in connector config.

## 6. Open design decisions

1. **Matter identity across sources** — practice-management import is authoritative
   when available (RA-MICRO/Actaport already know the matters); pure-inference mode
   needs a merge/split review surface in the UI eventually.
2. **Original-content caching** — the single-appliance MVP retains a content-addressed
   copy for crash-resume; production deployments need a configurable retain/refetch
   policy per source.
3. **Anonymization reversibility** — v0 stores no reverse mapping (safest); revisit if
   firms want privileged deanonymization.
4. **Graph store** — the Relation table is deliberately a plain typed-edge table in
   Postgres; a graph engine is an optimization we adopt only if query patterns demand
   it (see architecture doc).
