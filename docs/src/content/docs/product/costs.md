---
title: Costs
description: How model spend is measured and attributed per stage and per model, the declared per-token rates, CSV export, and the LEDES/UTBMS billing data extracted from indexed invoices.
---

The **Costs** page (admin-only) shows two unrelated kinds of money: what
LegalMemory itself spends on model calls, and the billing structure it
extracts from the firm's own invoice documents. Both are measured, never
estimated.

## Where the spend numbers come from

Every model call goes through the LiteLLM gateway, and every call funnels
through one client module (`pipeline/providers.py`). That module books **one
`UsageEvent` row per gateway response**:

- Token counts come from the response's `usage` block; a response without one
  is not billed.
- USD comes from the gateway's `x-litellm-response-cost` response header
  (falling back to `x-litellm-response-cost-original`). The gateway prices
  each call itself, from its cost map plus the deployment's declared rates.
- The row also records the stage that made the call, duration, the gateway
  call id as `trace_id`, and reasoning-token counts when the model reports
  them.
- The row is written on its **own short-lived session**, so a stage that
  later fails and rolls back still pays for the calls it really made.
  Accounting can never fail a model call; a systematic accounting failure is
  logged with its traceback rather than swallowed.
- The gateway never retries (`router_settings.num_retries: 0`), and the
  pipeline's own retries each make a real gateway call, so a stage is billed
  for every attempt it actually made and none it did not. An agentic stage
  books one row per gateway turn, not one per task.

Separately, the gateway keeps its own authoritative spend ledger in its
database (`LiteLLM_SpendLogs`), including full prompts and responses
(`store_prompts_in_spend_logs: true` in `deploy/litellm/config.yaml`), the
record a firm can be required to produce, and one of the stores captured by
[backups](/product/backup/) (`backup.sources.gateway_databases`). The Costs
page does not read it; it aggregates LegalMemory's own `UsageEvent` ledger.

`GET /api/costs` aggregates the newest 5,000 usage events and returns the
totals (USD, input/output tokens, calls), a per-model breakdown, a per-stage
breakdown, and the list of unpriced models.

## Per-stage attribution

Attribution is a context variable, not a parameter threaded through every
call: whoever owns a unit of work sets `usage_stage(...)`, and every gateway
call made inside that block, including tool loops and retrieval legs in
between, books under that stage.

| Ledger stage | Set by |
| --- | --- |
| `fetch` … `index` | Each pipeline stage claim, under its own stage name (`classify_matter`, `relate`, `extract_metadata`, `extract_decisions`, `index`, …). |
| `search` | Retrieval: query embedding and, when enabled, rerank calls. |
| `ask` | The planner and synthesis calls of `POST /api/ask`; its retrieval legs book under `search`. |
| `extract_billing` | Firm-billing extraction (below). |
| `gen_evals` | The RL-environment builder, which runs outside the insertion DAG but keeps the stage name the cost centre knows. |
| `unassigned` | Rows written with no owning stage. |

Independently of the ledger, pipeline stages tag each LiteLLM request with
`doc:<source object>`, `stage:<stage>`, and a never-recurring per-attempt
`trace:<id>`, so full-message gateway traces are filterable per document and
stage and addressable per attempt. These tags feed tracing, not the Costs
aggregation.

## Declared rates and the no-rate fallback

The gateway prices calls from its cost map, which has no entry for a
self-hosted or newly released model, so real tokens would be reported at zero
cost. The deployment therefore **declares** its contracted per-token USD
rates in the environment, wired into the gateway's `model_list`:

| Variable | Applies to |
| --- | --- |
| `KI_LLM_INPUT_COST_PER_TOKEN` | LLM input tokens |
| `KI_LLM_OUTPUT_COST_PER_TOKEN` | LLM output tokens |
| `KI_EMBEDDING_INPUT_COST_PER_TOKEN` | Embedding input tokens (output rate is fixed at `0.0`) |

When a model still has no rate, the header reports `0.0`; `/api/costs` lists
any model with calls but zero cost under `unpriced_models`. The page then
says so explicitly (token counts are measured, USD is not) and the
per-stage chart falls back to plotting input tokens instead of drawing every
stage as an empty dollar bar. A stage that consumed millions of tokens must
never look like zero work.

## The per-model table and alias attribution

Spend is recorded against the **alias** an assignment names, because that is all the
gateway reports back per response. For display, `/api/costs` resolves each
alias to its upstream model via the gateway's `/model/info` and groups by the
model that actually ran: aliases that resolve to the same model collapse into
one row, and each row carries `aliases`, the gateway names that routed to it. The
console renders this as "via `<aliases>`" under the model name, answering
"why is this model billed at all" without naming rows after pipeline stages
(the by-stage breakdown already does that, better).

If the gateway is unreachable at aggregation time, an alias keeps its own
name as `provider/alias` rather than being dropped; the console marks such a
row as a gateway alias whose model is unresolved. Each row shows calls, input/output
tokens, and either the cost or "no rate".

## CSV export

**Export CSV** builds the file client-side from the same `/api/costs`
payload, as `knowledge-index-costs.csv`, with the columns
`section,name,calls,input_tokens,output_tokens,cost_usd`: one `model` row per
per-model entry, one `stage` row per per-stage entry, and a final `total`
row.

## Firm billing (LEDES/UTBMS)

The second half of the page is structured legal data from the firm's own
documents, kept strictly apart from the model spend above. Billing is
relational data, not document metadata: it is never chunked into the search
index.

### Extraction pipeline

Documents get their type from the Extract-metadata stage's ontology walk;
billing extraction then operates on every document typed `invoice`:

1. `POST /api/actions/extract-billing` (admin; also the **Extract from
   invoices** button) runs the `BillingExtractor`.
2. For each invoice document it takes the **latest final version**, reads the
   stored converted text (first 16,000 characters), and extracts a typed
   `BillingExtraction` with the model assigned to the `extract_metadata` stage: deterministic field
   mapping, no regex. Spend books under the `extract_billing` stage.
3. Deduplication is layered: a document whose source object already produced
   an invoice is skipped up front; an invoice is unique per
   `(law_firm_id, invoice_number)`; a line item per
   `(invoice, line_item_number)`; a timekeeper per
   `(law_firm_id, ledes_timekeeper_id)`. A document the model judges not to
   be an invoice is counted, not inserted.
4. The run also promotes free-form client/party identifiers into typed
   `EntityIdentifier` rows (schemes such as `lei`, `de_hrb`, `vat_ustid`), the
   hard signal for entity resolution.
5. The result reports invoices and line items inserted, duplicates skipped,
   non-invoices, identifiers promoted, and errors.

### Tables

| Table | Contents |
| --- | --- |
| `billing_invoices` | One row per invoice (LEDES 1998B/XML shape): number, dates, client/matter links, totals, tax, currency, and the source object it came from. |
| `billing_line_items` | One row per LEDES line: line type (F/E/IF/IE), date, timekeeper, UTBMS task/activity/expense codes (e.g. `L110`, `A101`, `E101`), units, unit cost, total, description. |
| `timekeepers` | Billable people, deduplicated on the LEDES timekeeper id. |
| `entity_identifiers` | Typed identifiers per client/party, unique per (entity, scheme, value). |

Every inserted row carries provenance (extractor and, for invoices, the model
that produced it).

### Endpoints

All admin-gated:

| Endpoint | Returns |
| --- | --- |
| `POST /api/actions/extract-billing` | Runs the extractor; optional `limit` on inserted invoices. |
| `GET /api/billing/invoices` | Newest invoices with number, date, matter, total, currency, line count. |
| `GET /api/billing/rollup/{matter_id}` | A matter's invoiced total plus units and fees grouped per UTBMS task code. |
| `GET /api/entities/resolve?q=` | Clients/parties matched by name or identifier, including shared-identifier matches. |

### MCP billing tools

External clients query the same tables through the [MCP
server](/product/external-access/), not admin-gated, but ACL-scoped to the
caller and citation-fail-closed:

| Tool | Behaviour |
| --- | --- |
| `billing_rollup` | Same rollup as the API, restricted to matters the caller can see; fails closed if any invoice lacks an exact project/document/source-object citation. |
| `list_invoices` | A matter's invoices, each with its exact citation. |
| `resolve_entity` | Name/identifier resolution; results without an authorized citation are withheld. |

Every tool call is audited with the caller's principals and result counts.
