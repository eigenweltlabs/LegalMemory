---
title: What is LegalMemory?
description: What LegalMemory does, how it is put together, and what makes it different from a plain RAG stack.
---

LegalMemory is an open-source, on-premises knowledge index for law firms. It
continuously syncs the firm's document estate (DMS libraries, cloud drives,
practice-management systems, mailboxes, chat) into a **shadow index**: the
sources are only ever read, never modified, and every document's source
permissions are mirrored alongside its content.

On top of that index, LegalMemory does more than embed and retrieve:

- **Every document becomes structured data.** An insertion pipeline converts
  each file (including OCR for scans), classifies it into a matter by reading
  its folder neighbourhood and searching existing matters, links related
  documents (draft→final version chains, annexes, referenced contracts),
  extracts typed metadata from final versions, and captures anonymized decision
  rationale from correspondence and redlines.
- **Retrieval is permission-scoped and structure-aware.** Queries run as the
  calling person. Project grants and mirrored source ACLs are compiled into a
  filter that is applied *before* any ranking; hybrid lexical + vector legs are
  fused, version status decays superseded drafts, and results collapse to the
  best version of each document. See [how retrieval works](/concepts/retrieval/).
- **AI tools plug in over MCP.** An identity-bound MCP server exposes search,
  matter lookup, decision records, billing queries and entity resolution to any
  MCP-capable client, with an append-only access ledger recording every call.

## The appliance

LegalMemory ships as a Docker Compose stack assembled from proven open-source
components: PostgreSQL + pgvector as the system of record, OpenSearch for
ACL-scoped hybrid retrieval, Docling Serve for document conversion and OCR,
LiteLLM as the model gateway (cloud providers now, vLLM/TEI for air-gapped
installs), Hatchet for pipeline orchestration, and Keycloak + oauth2-proxy for
identity. The admin console (the product UI this documentation follows) runs
on the appliance itself.

Every capability is a real service: there are no mocks, no silent fallbacks. A
failing pipeline stage retries with backoff and quarantines; nothing degrades
silently.

## Where to go next

- [Quick start](/getting-started/quickstart/): bring the stack up and index a
  first source.
- [Product guide](/product/overview/): one page per screen of the admin
  console.
- [Connectors](/connectors/): connect each supported source.
- [Architecture](/concepts/architecture/): how sync, pipeline, retrieval and
  MCP fit together.
