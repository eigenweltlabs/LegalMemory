---
title: Connectors
description: Technical reference for the Connectors console — connection lifecycle, journey states, scoping, sync runs and the scheduler, deletion confirmation, event delivery, credential storage, and configuration.
---

**Connectors** is the console page that manages every connection between
LegalMemory and a document source — cloud drives, DMS and practice-management
systems, mailboxes, chat workspaces, and local folders. This page documents the
console feature and the sync machinery behind it. Per-provider setup steps
(OAuth app registration, provider-side scopes, troubleshooting) live in the
[connector guides](/connectors/); what happens to a document after sync is
documented under the [insertion pipeline](/product/pipeline/).

All connector management is admin-only. Non-admins see the connection list
(filtered to projects they can see) without actions.

## API surface

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/connectors/catalog` | GET | Catalog: native filesystem entries plus every registered connector and roadmap entry. |
| `/api/connectors/{kind}/fields` | GET | What the connect form must ask: auth fields, config fields from the connector's own schema, and the OAuth registration guide. |
| `/api/sources` | POST | Create a connection — or, for an OAuth kind, only a pending-authorization intent. |
| `/api/sources` | GET | List connections with derived state (scope, counts, pending deletion, event delivery). |
| `/api/sources/{id}` | DELETE | Disconnect and reclaim storage. |
| `/api/connectors/{source_id}/authorize` | POST | Re-authorize an existing connection whose grant expired or was revoked. |
| `/api/connectors/oauth/callback` | GET | Provider redirect target. Unauthenticated; authorization rests on the single-use `state`. |
| `/api/sources/{id}/browse` | GET | List a node's children with the connection's own credentials, for the folder picker. |
| `/api/sources/{id}/scope` | PUT | Replace the subtree roots a connection syncs. |
| `/api/actions/sync` | POST | Enqueue one orchestrated sync run per eligible source (202; never scans inline). |
| `/api/actions/pipeline` | POST | Start an insertion run. |
| `/api/fs/list` | GET | Browse server-visible directories for a mounted local folder. |
| `/api/fs/import-folder` | POST | Copy a browser-selected folder into managed storage (max 10,000 files, 512 MiB; unclaimed imports are swept after 30 minutes). |

## Connection lifecycle

`POST /api/sources` behaves differently by kind:

- **Local folders and plugin drop directories** (`local_fs`, `plugin_drop`):
  the `Source` row is created immediately (`201`, status `active`). The root
  must be an existing directory.
- **Token-authenticated connectors**: `access_token` is required; the `Source`
  row is created immediately and the token is written to the encrypted
  credential store in the same transaction.
- **OAuth connectors**: nothing is written to `sources`. The request validates
  the firm's `client_id`/`client_secret`, stores a **pending-authorization
  intent** (display name, project, config, sync policy, the client credentials
  and the PKCE code verifier) keyed by a single-use `state`, and answers `202`
  with `pending_authorization: true` and an `authorization_url`. The intent
  expires after 30 minutes. An operator who never signs in at the provider
  leaves nothing behind.

The **OAuth callback** (`GET /api/connectors/oauth/callback`) matches the
returned `state` in constant time, in this order:

1. **New connection.** The state claims a pending-authorization intent. The
   code is exchanged; on success the `Source` (status `active`), its encrypted
   credentials, and the deletion of the intent are committed in one
   transaction. The connection exists only from this point. On a failed
   exchange the intent is discarded (authorization codes are single-use) and
   nothing is created. If the project chosen at setup no longer exists, the
   handshake is discarded rather than creating an unfiled source.
2. **Re-authorization.** The state matches the `oauth_state` stored with an
   existing connection's credentials. On success the tokens are replaced and
   the source returns to `active`; on failure the state is burned and the
   source is marked `error`. An expired handshake answers `410` and burns the
   state. Re-authorization never duplicates a source, and abandoning it leaves
   a working connection working.

Either way the browser is redirected to `/?connected=<kind>#connectors`. The
console reads that marker, identifies the freshly connected source (same kind,
authorized, never synced, not yet scoped), and — for scoping-capable
connectors — opens the folder picker directly; if two candidates match it asks
instead of guessing.

`POST /api/connectors/{source_id}/authorize` starts a re-authorization for an
existing connection. It requires stored client credentials, writes a fresh
single-use state and PKCE verifier (with the same 30-minute deadline) into the
credential blob, and returns the provider URL. The source's status changes only
when the callback succeeds.

**Deleting a connection** removes its sync records, encrypted credentials,
mirrored group memberships, any pending-deletion watch, and its pipeline run
history; it then reclaims the connector staging tree, a browser-imported folder
it owns exclusively, and content blobs no other source still references (shared
blobs are kept and counted). Provider event subscriptions are removed upstream
best-effort before the local delete. Nothing is ever deleted at the source.
Documents and chunks derived from the source become unreachable — retrieval
re-verifies every hit against a live source object, so orphans fail closed.

## Journey states

The console derives one journey per connection from the source payload and the
run ledger (`journeyOf()` in the Connectors page). Connections short of
"searchable" get a **journey card** with the state, an explanation, and the
action that leaves the state. Conditions are evaluated top to bottom; the first
match wins.

| State | Console label | Condition | Unblocking action |
| --- | --- | --- | --- |
| `authorize` | never authorized | `status == "pending_auth"` (created before deferred OAuth creation existed, or grant withdrawn) | Authorize at the provider |
| `queued` | waiting | Sync run is `queued` with no `started_at` — reserved, no worker picked it up | None; if persistent, check the worker |
| `syncing` | syncing | Sync run active (`queued`/`running`); headline carries the live `observed` counter | None — in progress |
| `sync_failed` | sync failed | `status == "error"` or the latest sync run `failed`; nothing was removed from the index | Sync again; for OAuth kinds also Re-authorize |
| `scope` | never synced | Scoping-capable, no scope saved, never synced | Choose folders, or sync the whole source |
| `sync` | never synced | `last_sync_at` is null (scoped, or kind without a folder tree) | Sync now |
| `empty` | empty | Last sync completed with zero live objects | Sync again; change folders where the kind supports scoping |
| `access_refresh` | updating access | An active access-refresh run covers this source | None — in progress |
| `indexing` | indexing | An active insertion run with pending work for this source | None — in progress |
| `confirming` | confirming deletion | `pending_deletion.object_count > 0` (see below) | Sync again (each agreeing scan advances confirmation) |
| `index` | not indexed | Synced > 0, indexed = 0 | Run the insertion pipeline |
| `index_unknown` | not indexed | Fallback for payloads without the per-source `indexed_count`; per-connection split unknowable | Run the insertion pipeline |
| `partial` | not indexed | 0 < indexed < synced | Run the insertion pipeline |
| `ready` | searchable | indexed count has caught up with synced count | None; open the data explorer |

Notes on the model:

- The four journey steps are Connect → Scope → Sync → Index; the Scope step is
  marked "no folder tree" for kinds without `supports_scoping`.
- A run with no measured denominator (a full scan) reports `progress` 0
  throughout and publishes a running count in `current_step` instead; the
  console shows an indeterminate bar rather than a fake percentage.
- `ready` and `confirming` connections are excluded from the "setup in
  progress" stack: both have arrived; `confirming` has its own banner.

## Scoping

A scoping-capable connection carries **subtree roots**, not a flat folder
list: a root means that folder and everything below it, including sub-folders
created later. An empty roots list means the whole source.

**Browsing.** `GET /api/sources/{id}/browse?node=<id>` returns the children of
a node (top level when `node` is omitted) using the connection's own stored
credentials, so the tree shows exactly what the grant can reach. It answers
`422` for kinds that sync as a whole, `409` while the connection is still
`pending_auth`, and `502` with the provider's own message for upstream
failures — the console shows that message verbatim and leaves the saved
selection untouched. The tree is loaded lazily; the console pages each branch
60 rows at a time and its filter only searches branches already loaded. An
empty top level is reported as a real answer ("this account can open nothing
here"), not an error.

**Representation.** Roots are stored in `sources.config.connector.roots` as
`{id, type, title, metadata}`. `metadata` is the provider's own locator (drive
id, folder id); the connector's traversal needs it, and a root stored without
it silently syncs nothing. A separate `scope_decided` flag records that an
operator made an explicit choice — required because an empty list cannot
distinguish "whole source, deliberately" from "picker not reached yet", and
the scheduler must not crawl a freshly authorized estate behind an open folder
picker.

**Fingerprint.** The scope fingerprint is a BLAKE2b-128 digest of the sorted
root ids only. Reordering roots, or a provider returning different display
titles or metadata, is not a re-scope. The fingerprint the last sync ran under
is persisted on the source; a difference between stored and current
fingerprints is what the engine treats as a re-scope.

**Saving.** `PUT /api/sources/{id}/scope` replaces the roots, sets
`scope_decided`, and reports `changed` (fingerprint moved) and
`would_remove_existing` (changed while live objects exist). Saving in the
console immediately starts a sync, because nothing else would apply the
selection.

Re-scope behaviours:

| Change | Effect |
| --- | --- |
| **Narrowing** (roots removed, or a first selection on a source that already has objects) | The next sync is forced full and the tombstone guards are lifted for exactly that run — a re-scope is an instruction, not an observation. Documents outside the new selection are tombstoned on that sync, without deletion confirmation. The console requires an explicit "documents will be removed" confirmation before saving, but only when the source already contributed objects. |
| **Widening / clearing** (roots added, or emptied back to whole source) | The next sync is forced full — a delta token describes changes within the old selection and would never enumerate newly included folders. Nothing is deleted; previously tombstoned objects that reappear are restored. |
| **A configured root disappears at the provider** | Not a re-scope: the fingerprint is unchanged, so the guards stay on. The next full scan observes nothing beneath the root and the missing objects fall under ordinary deletion confirmation instead of being removed at once. |

## Sync execution

**Enqueue.** `POST /api/actions/sync` (optionally `{"source_id": ...}`) never
scans inline. For each eligible source it writes one `pipeline_runs` row
(workflow `source-sync`, status `queued`) and hands it to the configured
orchestrator — a Hatchet worker, or an in-process thread pool (4 workers) on a
single-VM deployment. The `202` response lists the started runs and, for every
source not started, a `skipped` entry with the reason (`source is paused`,
`awaiting authorization`, `a sync is already in flight for this source`). The
console repeats skip reasons verbatim. Before enqueuing, stranded runs (left
behind by a dead worker) are swept so they cannot block the source forever.

**One active run per source.** Reservation is serialized by a per-source
Postgres advisory transaction lock, and backstopped by
`uq_pipeline_runs_active_sync` — a partial unique index on
`pipeline_runs.source_id` where the workflow is `source-sync` and the status is
`queued` or `running`. Two concurrent crawls of one estate could interleave
tombstones with observations and delete documents that still exist; the
database refuses the second run whether it comes from a click, the scheduler,
the watcher, or another process.

**Run lifecycle.** `status` goes `queued → running → completed | failed`.
`current_step` moves through `queued`, `scan`, `scan (N observed)` (updated
every 50 observations on a separate DB connection, so progress is visible
before the scan's own transaction commits), `handoff`, and a final step
(`complete`, `complete (nothing new to insert)`, `complete (deletions
applied)`, `complete (insertion not automatic)`, or the failing step).
`progress` is 0 for the whole scan — a full scan has no denominator — and 1 on
completion. A failed run records `{class, message}` in `last_error` and marks
the source `error`; the engine never tombstones unless the crawl reaches EOF,
so a mid-scan crash can only have added or updated rows.

Run counters: `observed`, `created`, `changed` (byte changes; full pipeline),
`metadata_changed` (bytes identical; no model spend), `access_changed`
(grants changed; queues only the access projection), `unchanged`, `restored`,
`tombstoned`, `pending_deletions`, `batches`, `mode` (`full` or
`incremental`), `trigger`, and `insertion_run_id` once the handoff has started
insertion. Staged bytes are hashed (SHA-256) against the indexed content hash
so provider etag/mtime churn from renames and sharing changes does not re-run
conversion and embedding.

**Handoff.** With `pipeline.auto_insert_after_sync` (default on), a run that
produced `created`/`changed`/`access_changed`/`restored` work launches an
insertion run and records its id; the console then shows that insertion run on
the connection's card. A tombstone-only run completes without insertion —
tombstones become unretrievable in the scan transaction itself. With
auto-insert off, the run completes with a null `insertion_run_id` and the
journey card offers the pipeline button.

**Full vs incremental.** A connector with a delta capability syncs
incrementally from the stored cursor except when: the scope fingerprint
changed, a deletion is pending confirmation (only a full crawl can confirm
absence), or mirrored ACLs are stale (`security.acl_refresh_hours`, default
24 — a revocation at the source changes no document, so only a full scan
notices it). A completed full crawl also seeds the next delta cursor.

**Scheduler due-ness.** One scheduler thread in the app process ticks every
5–60 seconds and enqueues every source that is *due*: policy mode
`continuous` (an absent mode counts as continuous), status not `paused` or
`pending_auth`, no sync already in flight, and the last attempt older than the
policy interval. The last attempt is read from `pipeline_runs`
(`coalesce(finished_at, created_at)`, so a long scan spaces the next one from
its end) with `last_sync_at` as fallback — a failing source therefore retries
on its interval rather than every tick, and recovers on its own once the
credential is fixed. A never-synced, scoping-capable source is not due until
`scope_decided` is set, so a new estate is not crawled behind the open folder
picker. Intervals parse as `30s` / `5m` / `1h`, floor 5 seconds, default 300.
`KI_SYNC_SCHEDULE_SECONDS` caps the tick sleep; `0` disables scheduling
entirely.

**Triggers.** Every run records what started it: `api` (a click or API call),
`schedule` (the scheduler's tick), `watch` (the filesystem watcher — local and
mounted folders only, enqueued within about a second of a write, 500 ms
debounce), or a provider event waking the connector's delta sync. All four
paths converge on the same enqueue function and the same ledger, so they
cannot overlap; an event arriving while a run is in flight is coalesced, since
that run drains the same delta cursor.

## Deletion confirmation

A single scan cannot distinguish "the firm deleted this matter" from "the
connector enumerated one site out of forty". Deletions large enough to be
ambiguous are therefore **confirmed across scans** rather than applied or
refused.

After a full scan, the engine compares live objects against what the crawl
observed. The missing set is held for confirmation when:

- `observed == 0` while objects exist — the most suspicious shape; always
  held, regardless of source size; or
- the source has at least `MIN_OBJECTS_FOR_FRACTION_GUARD` (20) live objects
  and the missing fraction exceeds `max_tombstone_fraction` (default 0.5,
  `SyncEngine.MAX_TOMBSTONE_FRACTION`).

Never held: connectors with `verifiable_emptiness` (a mounted folder or plugin
drop directory — the listing *is* the estate), a source whose policy sets
`allow_empty_scan: true`, or a run under a changed scope fingerprint (a
re-scope is an explicit instruction).

The held state is the **set of missing external ids**, not a count
(`source_deletion_watch` + `source_deletion_candidates`). Each subsequent full
scan that reports the *identical set* increments `confirmations`; a different
set — objects came back, or different ones went missing — resets the claim to
1 and replaces the candidates. Totals are deliberately not compared: a
connector that loses 340 objects today and a different 340 tomorrow is
malfunctioning, and counting would average that into a deletion. Once
`confirmations` reaches `required` (default 3, from
`pipeline.deletion_confirmations`, per-source override
`sync_policy.deletion_confirmations`, re-read from live policy on every scan),
the tombstones are applied and the watch cleared. A trustworthy scan (below
threshold) also clears any stale watch. While a deletion is pending, the
engine forces full scans — a delta feed cannot confirm continued absence.

Meanwhile the documents stay indexed and keep answering searches. The console
shows a banner and the `confirming` state on the connection: how many
documents look deleted, confirmation progress ("2 of 3 syncs"), that nothing
has been removed yet, and that a permission withdrawn at the source looks
exactly like this. The same numbers travel on the run
(`pending_deletions`, `deletion_confirmations`,
`deletion_confirmations_required`) and the source payload
(`pending_deletion`). Deleting the connection clears its watch.

Setting `deletion_confirmations` to 1 tombstones on the first scan that
reports the loss — only for deployments that prefer losing index entries to
retaining deleted ones.

## Event delivery

`event_delivery` on every source payload reports how change detection works
for that connection. Events only wake the connector's normal delta sync; they
never write indexed state directly. The policy interval always remains as the
reconciliation safety net.

| Status | Produced when |
| --- | --- |
| `not_supported` | The connector has no event adapter; the policy interval drives sync. |
| `reconciliation_only` | The adapter cannot cover this scope live (e.g. Google permits no subscription on the whole My Drive root); the delta cursor still makes scheduled reconciliation incremental. |
| `waiting` | Subscriptions cannot be created yet — scope not decided, or the first sync has not discovered the drives/libraries to subscribe to. |
| `unconfigured` | The provider supports live events for this scope, but the appliance-side transport (Google Pub/Sub, or Azure Event Hubs) is not configured. |
| `pending` | Transport configured; not every desired subscription is active yet. |
| `error` | A subscription create/renew failed; the recorded error is shown as the detail. |
| `active` | Every desired subscription is active; the detail names the mechanism and the interval's reconciliation role. |

Adapters exist for SharePoint Online, OneDrive (both Microsoft Graph →
Azure Event Hubs) and Google Drive (Workspace events → Pub/Sub); see
[Microsoft live events](/connectors/microsoft-live-events/) and
[Google Drive live events](/connectors/google-drive-live-events/). The event
manager renews subscriptions in the background and removes them upstream when
a connection is deleted (best-effort; failures are reported in the delete
response, and provider subscriptions expire on their own).

## Credential storage

Connector secrets never enter `sources.config`, which is documented as
non-secret, and never appear in logs. They live in
`source_credentials.payload` as AES-256-GCM ciphertext
(base64 `nonce || ciphertext`, 12-byte nonce) under the 32-byte key supplied
out of band in `KI_CONNECTOR_CREDENTIAL_KEY` (urlsafe base64). The key is
required: a missing or malformed key is a hard startup error, never a
plaintext fallback.

- `sources.config` holds non-secret configuration: root path, connector
  settings, scope roots, default ACL.
- `source_credentials` holds the encrypted blob (client id and secret, access
  and refresh tokens, and — transiently — the single-use OAuth state and PKCE
  verifier of an in-flight re-authorization), plus a 4-byte key fingerprint,
  the provider name, and an informational token expiry.

Rotating-refresh providers invalidate the old refresh token the moment a new
one is issued, so the token provider persists rotations from inside its
refresh path — a crash mid-sync cannot leave a dead connection.

**Rotation.** Each row records the fingerprint of the key that wrote it. If
`KI_CONNECTOR_CREDENTIAL_KEY` changes, loading fails with a diagnosis naming
both fingerprints; the fix is to restore the original key, or re-authorize the
connection so fresh credentials are written under the new key. There is no
automatic re-encryption. The OAuth callback skips rows it can no longer
decrypt rather than letting one unreadable row block every handshake.
Credentials are deleted with their connection.

## The catalog

The "Add a connection" grid has three tiers:

1. **Connectable in the console**: local folders (`local_fs`, via the system
   dialog import or a mounted server path) plus the launch connectors —
   SharePoint Online, Google Drive, OneDrive, and Clio. These open the connect
   form.
2. **Registered but not yet offered**: the remaining implemented connectors
   are shown greyed out ("Not available yet") and cannot be connected from the
   console.
3. **Planned**: inert roadmap cards (legal DMS and practice-management
   systems). They carry only a name, category and note — no capability rows,
   because there is no implementation to describe — and are not clickable.

The `plugin_drop` kind (a drop-directory contract for custom DMS integrations
built by a forward-deployed engineer) is addressable through the API but
marked internal and hidden from the grid. Cards state each connector's
capabilities from the registry: incremental mode, whether source permissions
are mirrored (`mirrors_acls` — a connector without it produces documents
nobody can retrieve until access is granted in LegalMemory), and whether it
supports folder scoping.

## Private-corpus guard

Connectors flagged `private_corpus` (mailboxes and personal drives: OneDrive,
Outlook Mail, Gmail) index one person's corpus. Creating such a connection
with a `default_acl` naming a `group:` or `role:` principal is refused with
`422` unless the request carries `confirm_broad_grant: true` — that grant
would publish one person's entire mailbox or drive to everyone in the group.
The connect form shows the warning next to the grant field and requires an
explicit confirmation checkbox before it will submit the flag. A `user:`
principal, or an empty grant (mirrored permissions decide — normally the owner
alone), needs no confirmation.

## Configuration

Settings live under `connectors` in the app configuration; environment
overrides use the `KI_` prefix with `__` as the nesting delimiter.

| Setting | Environment variable | Default | Effect |
| --- | --- | --- | --- |
| `connectors.public_base_url` | `KI_CONNECTORS__PUBLIC_BASE_URL` | `http://localhost:8000` | Public base URL of the appliance. The OAuth redirect URI is derived from it and must exactly match what the firm registered at the provider. |
| `connectors.oauth_callback_path` | `KI_CONNECTORS__OAUTH_CALLBACK_PATH` | `/api/connectors/oauth/callback` | Path component of the derived redirect URI. |
| `connectors.allow_private_hosts` | `KI_CONNECTORS__ALLOW_PRIVATE_HOSTS` | `false` | Permits connectors to reach private-network hosts. Off by default: the SSRF guard stops a connector being steered at the appliance's own services. Enable only for sources genuinely hosted on the firm's LAN. |
| `connectors.events.enabled` | `KI_CONNECTORS__EVENTS__ENABLED` | `true` | Master switch for provider-event delivery. |
| `connectors.events.reconcile_seconds` | `KI_CONNECTORS__EVENTS__RECONCILE_SECONDS` | `300` (30–3600) | Interval of the event manager's subscription reconcile/renew pass. |
| `connectors.events.google_drive.topic` | `KI_CONNECTORS__EVENTS__GOOGLE_DRIVE__TOPIC` | empty | Pub/Sub topic Workspace events publish to. |
| `connectors.events.google_drive.pull_subscription` | `KI_CONNECTORS__EVENTS__GOOGLE_DRIVE__PULL_SUBSCRIPTION` | empty | Pub/Sub pull subscription the appliance consumes. |
| `connectors.events.google_drive.service_account_file` | `KI_CONNECTORS__EVENTS__GOOGLE_DRIVE__SERVICE_ACCOUNT_FILE` | empty | Path to the consuming service account's JSON key (alternative to the env-var form below). |
| `connectors.events.google_drive.service_account_json_env` | `KI_CONNECTORS__EVENTS__GOOGLE_DRIVE__SERVICE_ACCOUNT_JSON_ENV` | `KI_GOOGLE_EVENTS_SERVICE_ACCOUNT_JSON` | Name of the environment variable that holds the service-account JSON itself; the secret never enters config or the admin API. The Drive transport counts as configured only when topic, pull subscription and one credential form are all present. |
| `connectors.events.microsoft_graph.notification_url` | `KI_CONNECTORS__EVENTS__MICROSOFT_GRAPH__NOTIFICATION_URL` | empty | The `EventHub:` URL Microsoft Graph writes notifications to. |
| `connectors.events.microsoft_graph.fully_qualified_namespace` | `KI_CONNECTORS__EVENTS__MICROSOFT_GRAPH__FULLY_QUALIFIED_NAMESPACE` | empty | Event Hubs namespace the appliance consumes from (kept separate from the notification URL so a portal value cannot land in the wrong field). |
| `connectors.events.microsoft_graph.event_hub_name` | `KI_CONNECTORS__EVENTS__MICROSOFT_GRAPH__EVENT_HUB_NAME` | empty | Event hub name. |
| `connectors.events.microsoft_graph.consumer_group` | `KI_CONNECTORS__EVENTS__MICROSOFT_GRAPH__CONSUMER_GROUP` | `$Default` | Consumer group. |
| `connectors.events.microsoft_graph.tenant_id` | `KI_CONNECTORS__EVENTS__MICROSOFT_GRAPH__TENANT_ID` | empty | Entra tenant of the consuming app. |
| `connectors.events.microsoft_graph.client_id` | `KI_CONNECTORS__EVENTS__MICROSOFT_GRAPH__CLIENT_ID` | empty | Client id of the consuming app. |
| `connectors.events.microsoft_graph.client_secret_env` | `KI_CONNECTORS__EVENTS__MICROSOFT_GRAPH__CLIENT_SECRET_ENV` | `KI_MICROSOFT_EVENTS_CLIENT_SECRET` | Name of the environment variable holding the consuming app's client secret. |

Related settings referenced above, outside `ConnectorsConfig`:

| Setting | Environment variable | Default | Effect |
| --- | --- | --- | --- |
| — | `KI_CONNECTOR_CREDENTIAL_KEY` | required | 32-byte urlsafe-base64 AES-256-GCM key for the credential store; must be identical in app, worker and watcher. |
| — | `KI_SYNC_SCHEDULE_SECONDS` | `60` | Cap on the scheduler's tick sleep; `0` disables interval scheduling entirely ("continuous" then means nothing). |
| `pipeline.auto_insert_after_sync` | `KI_PIPELINE__AUTO_INSERT_AFTER_SYNC` | `true` | Whether a sync with new work launches insertion itself. |
| `pipeline.deletion_confirmations` | `KI_PIPELINE__DELETION_CONFIRMATIONS` | `3` (1–20) | Consecutive agreeing full scans required before a guarded deletion is applied. |
| `security.acl_refresh_hours` | `KI_SECURITY__ACL_REFRESH_HOURS` | `24` | Maximum age of mirrored permissions before a full scan is forced on an otherwise-incremental source. |

Per-source overrides in `sync_policy` (JSON on the source): `mode`
(`continuous`/`manual`), `interval`, `allow_empty_scan`,
`max_tombstone_fraction`, `deletion_confirmations`.
