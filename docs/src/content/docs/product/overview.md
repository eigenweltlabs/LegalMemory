---
title: Overview
description: The console's landing page — ACL-scoped index metrics, the attention panel and the exact conditions it detects, admin actions, and the command palette's search legs.
---

**Overview** is the console's landing page. It is built from four requests:
`GET /api/status` and `GET /api/sources` (any signed-in user), and
`GET /api/runs?limit=15` and `GET /api/components` (requested only when the
caller is an administrator). **Refresh** re-issues the status, sources, and
runs requests; nothing on the page polls on its own.

## Metric tiles

The five tiles render `counts` from `GET /api/status`. Every count is computed
under the caller's own principals, so an administrator and a member can
legitimately see different numbers. The scoping rule per tile:

| Tile | Query behind it | ACL scoping |
| --- | --- | --- |
| Documents | `len(visible_document_ids)`; the note shows `counts.chunks`, the number of `Chunk` rows whose `document_id` is visible | `AccessService.visible_document_ids(principals)` |
| Matters | `COUNT(DISTINCT Document.matter_id)` over visible documents with a non-null `matter_id` | Counted through readable documents, deliberately **not** through project membership — access normally comes from mirrored source ACLs, and a firm can run with no projects at all |
| Connected sources | Count of visible `Source` rows; the note shows `counts.source_objects`, the non-deleted `SourceObject` rows under those sources | Same rule as `GET /api/sources`: an admin sees every source; anyone else sees sources that are unowned (`project_id IS NULL`) or sit in a project they can reach |
| Projects | `len(visible_project_ids)` | `AccessService.visible_project_ids(principals)` |
| Quarantined | `ProcessingState` rows with status `quarantined`, joined to non-deleted source objects | Joined through the caller's visible sources — a quarantined document the caller cannot read is not counted at them |

`/api/status` also returns two things the tiles do not show: a per-stage
`pipeline` breakdown in which stored `skipped` states are split into `waiting`
(parked behind an unfinished predecessor stage) and `disabled` (stage switched
off in config), and `runs` — up to 8 `queued`/`running` pipeline run records.
Serving the endpoint also sweeps stranded runs (`_sweep_runs_if_due`), so a run
nothing will ever advance stops being reported as in flight.

## The attention panel

The "Needs attention" panel synthesizes every actionable condition into one
list. Each row is a button that navigates to the page that fixes it, with a
`focus` parameter where a specific source is involved.

| Condition | Trigger (as implemented) | Data read | Row links to |
| --- | --- | --- | --- |
| Failed at a stage | Any stage in `status.pipeline` with a non-zero `failed` bucket → "N failed at *Stage*" | `/api/status` `pipeline` | Pipeline |
| Quarantined | `counts.quarantined > 0`. Rows quarantined after retries; nothing retries them automatically | `/api/status` `counts` | Pipeline |
| Waiting, nothing running | Only when no run is in flight: the sum of `waiting` + `pending` across all stages is non-zero | `/api/status` `pipeline` and `runs` | Pipeline |
| Source not syncing | `source.status` is one of `error`, `failed`, `unreachable`, `sync failed` (case-insensitive) | `/api/sources` | Connectors, focused on the source |
| Never synced | `source.last_sync_at` is empty — nothing from that connection is searchable yet | `/api/sources` | Connectors, focused on the source |
| Pending deletion | `source.pending_deletion.object_count` is non-zero: the source reported objects gone, but they still answer searches until the deletion is confirmed | `/api/sources` | Connectors, focused on the source |
| Run failed in the last 24 h | A run from `/api/runs` with `status === "failed"` whose `finished_at` (or `started_at`) is within 24 hours. Older failures are history, not action items | `/api/runs?limit=15` (admin only) | Pipeline |

The failed-run row's detail comes from the run's `error` field, which the
orchestrator writes as JSON (`{class, message, …}`) but which can also arrive
as a plain string; either way it is rendered (truncated to 160 characters), and
a run with no recorded error reads "Stopped at *current_step*".

When the list is empty the panel shows a single quiet line with the most recent
`last_sync_at` across all visible sources and the mirrored object count.
In-flight runs render underneath the list with their current step and a
progress bar, regardless of whether anything needs attention.

## Admin actions

- **Run insertion pipeline** — `POST /api/actions/pipeline` (administrator
  only). It creates a `pipeline_runs` record and triggers one insertion run
  through the configured orchestrator: under the `hatchet` provider it triggers
  the workflow and returns `{run_id, provider, provider_run_id, status: "queued"}`;
  under `local` it runs in-process. An orchestrator that rejects the trigger
  leaves the run record in `failed` with the cause and the endpoint answers 502
  (400 for an unknown provider).
- **Services** — the first five entries of `GET /api/components`
  (administrator only): Model gateway (LiteLLM), Document parsing (Docling
  Serve), Search index (OpenSearch), Pipeline orchestrator, and Traces
  (Langfuse). Each is probed live with a 2-second HTTP GET; any HTTP answer —
  even 401 or 404 — counts as `ok`, a missing URL is `disabled`, a transport
  error is `unreachable`. Non-admins see a note that service endpoints are
  hidden from project members.

The Projects panel shows the first six results of `GET /api/projects` (already
ACL-scoped), linking into Data and Access.

## The command palette

`⌘K` / `Ctrl+K` opens the palette from any page. Input is debounced 220 ms and
nothing is queried below two characters. Every leg runs under the caller's own
principals; the palette never decides who may see what.

| Leg | Request | Limits shown | Scoping |
| --- | --- | --- | --- |
| Pages | none — the nav list is filtered in the browser | all matches | n/a |
| Documents | `GET /api/graph?query=…&limit=40`, keeping nodes with `kind === "document"` (title matches) | top 6 | Graph projection is ACL-scoped server-side |
| Matters | `GET /api/matters?query=…&limit=8` | top 4 | Scoped through the caller's readable documents (the graph leg matches only `Document.title`, so matters get their own lookup) |
| In content | `POST /api/search` with `{query, limit: 6}`; hits whose document already matched by title are dropped | top 5 | Hybrid search with ACL filtering before ranking |
| Connections | none — `GET /api/sources` is fetched once when the palette opens and filtered in the browser | top 4 | The sources endpoint applies the visibility rule server-side |
| People & groups | none — `GET /api/principals` is fetched once when the palette opens and filtered in the browser | top 5 | Administrator-only endpoint; the leg simply returns nothing for members |

The three network legs run in parallel per pause (`Promise.allSettled`); a
single failing leg degrades that group rather than the whole palette.
