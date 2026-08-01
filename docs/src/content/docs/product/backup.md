---
title: Backup
description: Technical reference for full-appliance backups — run model, per-store capture, encryption framing, destinations, verification, retention, and the staged restore.
---

The **Backup** page in the console configures and runs full-appliance backups of LegalMemory and restores from them. The unit of backup is the whole appliance: every backup is a set of components (database dumps, a search-index snapshot, file archives, a secrets capture) written together under one id and described by one manifest. A directory without a manifest is treated as an incomplete backup and is never offered for restore.

This page is the reference for how the feature behaves. The operator-facing walkthrough (CLI commands, whole-stack restore procedure) is [Backup & restore](/operations/backups/).

## Run model

A backup is a run on the same `pipeline_runs` ledger as sync and insertion, with `workflow = "backup"`. `POST /api/actions/backup` only *reserves* the run — one row with status `queued` — and returns `202` immediately; the work is then executed by the orchestrator worker (Hatchet) or, with the `local` orchestrator, in a background thread of the enqueuing process. Progress, counters and errors are recorded on the run row and served by `GET /api/runs`, which is what the console polls while a run is in flight.

Exactly one backup (or restore) may be active per appliance. The reservation takes a Postgres advisory transaction lock (`backup:appliance`) and checks for any run in `queued` or `running` inside the same transaction; a second request is refused with HTTP `409`, not queued.

| Run state | Meaning |
| --- | --- |
| `queued` | Reserved; not yet picked up by a worker. |
| `running` | Capturing, transferring, writing the manifest, verifying, or pruning. `current_step` names the stage. |
| `completed` | Every step finished. `current_step` is `complete`, or `complete (with warnings)` when optional stores were skipped. |
| `failed` | The cause is recorded in the run's error field (exception class and message). |

Run counters include `backup_id`, `trigger` (`api` or `schedule`), `force`, `components_captured` / `components_planned`, `bytes_stored`, `bytes_plaintext`, `warnings`, `verified`, `pruned`, and `seconds`. The progress fraction is `components_captured / components_planned`, where the denominator counts only components the plan says will actually be captured.

Backup ids are `ki-backup-YYYYMMDDTHHMMSSZ` (UTC). Lexical order is chronological order, which is what retention sorts on.

### Restored ghost runs

The dump of the appliance's own database is taken while the backup run is itself `running`, so every archive contains a ledger with one backup in flight — its own. Restoring that ledger brings the row back as an active run that no worker owns, and because only one backup may be active, that row would refuse every later backup and restore. The reservation path detects this: an active backup row that names a staging directory (`staging_dir` counter) which does not exist on this appliance's disk is failed with error class `RestoredRun` before the active-run check. The console renders these as cancelled ("Not a real run"), not as failures.

## What a run does, step by step

1. **Reserve.** One ledger row; refuse if a backup or restore is already active (after releasing any restored ghost runs).
2. **Settled-pipeline check.** If `require_settled_pipeline` is on and the run was not forced, the run fails when any document is in processing state `pending`, `running` or `failed` — a backup taken then holds a database that references artifacts the blob store has not finished writing. `force: true` (the console asks for confirmation) skips this check for this run only; the nightly schedule never forces.
3. **Destination preflight.** The destination is built and `check_writable()` is called: a probe object is written, read back and deleted. This happens before anything is dumped, so an unmounted share fails the run in seconds rather than after a long dump.
4. **Decide who encrypts and compresses.** If the destination encrypts for itself (restic), the appliance does not seal components and disables compression, because both would defeat the destination's deduplication. Otherwise, if `encrypt` is on, the backup key is loaded from the appliance's secret store.
5. **Capture, transfer, delete — one component at a time.** Each store is captured into a per-run staging directory (`KI_BACKUP_STAGING_DIR`, default `/data/backup-staging/<backup-id>`, created mode `0700`), hashed, streamed to the destination (encrypted on the fly — no ciphertext copy touches disk), and its staged file deleted immediately. The staging disk therefore only ever needs room for the largest single component. Any component whose plaintext exceeds `max_component_gb` fails the run.
6. **Write the manifest, last.** `manifest.json` and then `SHA256SUMS` (which covers the manifest) are written after every component. An interrupted run leaves a directory without a manifest, which listing reports as incomplete rather than restorable.
7. **Verify.** If `verify_after_write` is on, every component is read back from the destination and re-checked (see [Verification](#verification)). A backup that fails verification fails the run; it is not reported as a success with a note.
8. **Prune.** If `retention.prune_enabled` is on, the retention rules are applied. The backup just written is explicitly protected regardless of the rules.

The staging directory is deleted in a `finally` block, whatever happened — for the length of a run it holds a store of the appliance in plaintext.

### Partial failure

Only two things are fatal to a run: the appliance's own database (`postgres/ki`) and the destination. Every other store degrades: if it cannot be captured, the run continues, the gap is recorded as a warning in the manifest and on the run row, and the run ends `complete (with warnings)`. The distinction is deliberate — "the backup succeeded" and "the backup contains Langfuse" are different claims, and both are reported.

## Components

Components are captured in this order: databases, the search index, file sets, the appliance configuration, deployment secrets.

| Component | Captured how | Toggle (`backup.sources.*`) | On capture failure |
| --- | --- | --- | --- |
| `postgres/ki` | `pg_dump --format=custom` over the network | none — always captured | **Run fails** |
| `postgres/litellm` | `pg_dump`, URL derived from the primary server | `gateway_databases` | Warning, skipped |
| `postgres/langfuse` | `pg_dump`, URL derived from the primary server | `gateway_databases` | Warning, skipped |
| `postgres/hatchet` | `pg_dump`, URL from `KI_BACKUP_HATCHET_DATABASE_URL` | `orchestrator_database` | Warning, skipped |
| `opensearch/snapshot` | OpenSearch snapshot API, then a tar of the snapshot repository | `search_index` | Warning, skipped |
| `files/artifact-blobs` | tar of the content-addressed blob store | `artifact_blobs` | Warning, skipped |
| `files/uploaded` | tar of the browser-uploads directory | `uploaded_files` | Warning, skipped |
| `files/connector-staging` | tar of mid-sync scratch | `connector_staging` (off by default) | Warning, skipped |
| `files/watched` | tar of the watched-folders records | `watched_folders` | Warning, skipped |
| `files/extra-N` | tar of each entry in `extra_paths` | `extra_paths` | Warning, skipped |
| `volumes/keycloak` | tar of Keycloak's data volume, via a read-only mount | `identity_volume` | Warning, skipped |
| `volumes/hatchet-config` | tar of Hatchet's generated config volume, via a read-only mount | `orchestrator_config_volume` | Warning, skipped |
| `files/appliance-config` | tar of the top-level files of the data directory (`config.json`) | none — always captured | Warning, skipped |
| `secrets/environment` | JSON capture of deployment environment variables | `environment_secrets` (requires encryption) | Warning, skipped |

### Databases

All four databases are reached over the wire with `pg_dump`, never by copying data directories. Details that matter operationally:

- **Custom format** (`--format=custom`), because it is what `pg_restore` can restore selectively, in parallel, and into a differently named database.
- `--no-owner --no-privileges`, so a dump restores as whichever superuser the recovery environment has, without recreating role names first.
- Compression level 6, or **0 when the destination deduplicates** — a compressed dump turns a small row change into a completely different byte stream, which a content-defined chunker cannot see through.
- The password is passed in `PGPASSWORD`, never on the command line, so it does not appear in process listings.
- Timeout: `KI_BACKUP_PG_DUMP_TIMEOUT_SECONDS` (default 21600 s) per database; exceeding it fails that component.
- The LiteLLM and Langfuse databases live on the primary Postgres server and their URLs are derived from `KI_DATABASE_URL` (same server, different database name) unless overridden with `KI_BACKUP_LITELLM_DATABASE_URL` / `KI_BACKUP_LANGFUSE_DATABASE_URL`. Hatchet's database is a separate server and must be given explicitly.

### Search index

The snapshot API is the only supported way to copy a live OpenSearch cluster; a tar of a running node's data directory is a copy of a moving target. The run:

1. Registers (or re-registers) an `fs` snapshot repository named `KI_BACKUP_OPENSEARCH_REPO_NAME` (default `ki-backup`) at the path OpenSearch knows the shared volume by (`KI_BACKUP_OPENSEARCH_REPO_CONTAINER_PATH`). OpenSearch only accepts a location listed in its own `path.repo`.
2. Deletes any stale snapshot with this backup's name (left by a run that died mid-flight).
3. Takes a snapshot of `*,-.*` (every index except OpenSearch's internal dot-prefixed ones) with `include_global_state: true`, so analyzers, mappings and index templates come back with the data.
4. Polls every 5 s until the snapshot leaves `IN_PROGRESS`/`STARTED`; anything other than `SUCCESS` fails the component. Timeout: `KI_BACKUP_OPENSEARCH_TIMEOUT_SECONDS` (default 10800 s).
5. Tars the quiescent repository directory as read from the appliance's own mount of the same volume (`KI_BACKUP_OPENSEARCH_REPO_PATH`).
6. Deletes the snapshot from the repository afterwards — in a `finally`, whether the archive succeeded or not — so every backup is a self-contained full copy and the repository volume does not grow by a copy of the index per night.

### File sets

Directory components are tarred with sorted entries, so two backups of an unchanged directory differ only in gzip timestamps (and not at all when written uncompressed to a deduplicating destination). A file that disappears between the directory walk and the read is skipped, not fatal — a live appliance may delete its own temporary files mid-backup. A directory that does not exist yet but whose parent does is captured as an empty archive, recording "covered and empty" rather than "unreachable". Compression is gzip level 6, or none when the destination deduplicates.

`files/appliance-config` archives only the files sitting directly in the data root — which is where `config.json` lives — and none of its subdirectories, because those are captured as their own components and the staging/restore scratch directories must never be swept in.

### Deployment secrets

`secrets/environment` captures every environment variable whose name starts with `KI_`, `LITELLM_`, `HATCHET_`, `KEYCLOAK_`, `POSTGRES_` or `LANGFUSE_`, minus `KI_BACKUP_ENCRYPTION_KEY` and `KI_BACKUP_S3_SECRET_ACCESS_KEY` (a deployment that exported either for its own reasons must not have it swept into the backup that key protects). The payload is written `0600` from the moment it exists, and configuration validation refuses to enable this source unless the backup is encrypted (by the appliance or by a restic destination) — there is no path by which these secrets land on a share in the clear. The manifest records only the variable *names*.

The one variable that matters most is `KI_CONNECTOR_CREDENTIAL_KEY`. If it is not set in the capturing container, the component is still captured but the manifest carries a warning that the one secret a restore cannot do without is missing.

## Encryption

### The key

Backups are encrypted with AES-256-GCM under a 32-byte key. The key is set in the console (Backup page, step 2 of setup): **Generate** creates one server-side, shows it exactly once behind a copy-gated dialog, and stores it; or an existing base64url key can be pasted. A pasted value that is not valid base64 of exactly 32 bytes is rejected at save time. Headless deployments use `ki backup-key --generate` / `--set`.

Storage and disclosure rules, all enforced in code:

- The key (and the S3 credentials) are stored in the appliance's database, encrypted at rest under `KI_CONNECTOR_CREDENTIAL_KEY` with the same primitive and envelope as connector OAuth tokens. There is no environment-variable path for the backup key.
- No API ever returns a secret's value — only whether it is set and an 8-byte BLAKE2b fingerprint. The single exception is the moment of generation.
- The key's fingerprint is recorded in every manifest, so "is this the right key for this backup" is answerable before any decryption is attempted.
- Replacing the key does not re-encrypt existing backups; they still need the old key.

### Chunked sealing and why truncation fails closed

Components are tens to hundreds of gigabytes, so they are sealed as a framed stream rather than one GCM message: magic bytes `KIBAK1\n`, a length-prefixed JSON header, then length-prefixed sealed chunks of 4 MiB plaintext each. The header carries the algorithm, chunk size, a random 8-byte nonce prefix, the key fingerprint, and a **context** naming which component of which backup this stream is.

Each chunk is sealed with nonce `prefix || counter` and authenticated against additional data consisting of the SHA-256 of the header, the chunk's index, and a flag saying whether it is the final chunk. Consequences:

- **Truncation fails closed.** The last chunk is authenticated *as being last* (the final flag is decided by read-ahead during sealing, not by a trailer). A stream cut short by a full disk, a killed transfer, or deliberate shortening ends without a chunk that authenticates as final, and decryption raises instead of producing a shorter, plausible-looking dump. A stream that continues past its final chunk is also refused.
- **Chunks cannot be reordered or spliced** — each is bound to its index.
- **Components cannot be substituted between backups.** `manifest.json` and `SHA256SUMS` are not encrypted or signed, so someone who can write to the destination can rewrite both — but the substituted component's own header says which backup and which component it was sealed as, that header is bound into every chunk's tag, and it cannot be recomputed without the key. Staging and verification check the context against what the manifest claims and refuse a mismatch.
- **A wrong key is named, not guessed at**: the header fingerprint is compared before any chunk is opened, and the error states both fingerprints.

An empty input still produces one (empty, final) chunk, so a zero-length component is distinguishable from a stream that never started. The manifest is deliberately left readable without the key: it carries names, sizes, digests and fingerprints only, so an operator at the destination can see what a backup holds and whether they have the right key before decrypting anything.

When the destination is a restic repository, the appliance seals nothing: restic encrypts and authenticates every chunk itself, under the same stored backup key used as the repository password. The manifest then records `"performed_by": "destination"` rather than a null encryption block.

### The connector credential key

`KI_CONNECTOR_CREDENTIAL_KEY` is a different key with a different job: connector OAuth tokens (and the backup secrets themselves) are stored in the database as ciphertext under it. A database dump restored under a *different* connector key appears to work until the first token refresh, at which point every connector is dead. Three mechanisms address this:

1. Every manifest records the fingerprint of the connector key the dump was taken under.
2. `restore_plan` compares it with the deployment's current key and reports a mismatch as a **blocker** — the restore is refused, with instructions to set the key from the backup's `secrets/environment` component first.
3. The `secrets/environment` component carries the key's value (encrypted), so a recovery onto fresh hardware can recover it.

## Destinations

One destination is configured under `backup.destination`. All three kinds share the same contract — write a stream, read a stream, list backup ids, delete a backup, prove writability — so manifests, verification and retention behave identically whichever is configured.

| Field | Applies to | Default | Semantics |
| --- | --- | --- | --- |
| `kind` | all | `local` | `local`, `s3`, or `restic` |
| `path` | local, restic | `/backups` | Directory backups are written under. For restic without an explicit repository: the repository's parent directory. |
| `bucket` | s3, restic | `""` | Bucket name. Required for `s3`; for `restic` it selects an S3 repository. |
| `endpoint_url` | s3, restic | `""` | S3-compatible endpoint; empty means AWS's own endpoint for the region. |
| `region` | s3, restic | `us-east-1` | Passed to the client / `AWS_DEFAULT_REGION`. |
| `use_path_style` | s3 | `true` | Path-style addressing; MinIO and most on-prem gateways need it. |
| `prefix` | all | `knowledge-index` | Every backup lands under `<prefix>/<backup-id>/`; for restic it is the repository directory or key prefix. |
| `restic_repository` | restic | `""` | Explicit repository string (`sftp:`, `rest:`, `azure:`, …). Empty derives one: `<path>/<prefix>`, or `s3:<endpoint>/<bucket>/<prefix>` when a bucket is set. |
| `restic_host` | restic | `knowledge-index` | `--host` on every snapshot, so grouping is stable across container restarts. |
| `restic_no_cache` | restic | `false` | Passes `--no-cache`; for containers recreated nightly or read-only root filesystems. |

Credentials never appear in `config.json`. Three named secrets are set on the console page and held encrypted in the database: `encryption_key`, `s3_access_key_id`, `s3_secret_access_key`.

**`local`** — a directory, in practice a mounted NAS/SMB share (`KI_BACKUP_MOUNT` in the compose file mounts it at `/backups`). Stores whole objects. Writes go to a temporary file in the target directory, are fsynced, renamed into place, and the directory is fsynced, so an interrupted run leaves no half-file a listing would count as a component. Component keys from a manifest are resolved against the backup root and refused if they escape it.

**`s3`** — any S3-compatible endpoint. Requires `boto3` (`pip install 'knowledge-index[s3]'`, imported lazily). Both access key and secret key are required; there is deliberately no fall-through to anonymous access or instance roles. Uploads stream via `upload_fileobj` (multipart above the SDK's threshold), with 5 standard-mode retries.

**`restic`** — a restic repository driven through the restic binary; the destination that stores a night as the difference from the night before. Requires restic ≥ 0.14 (the first release whose repository format compresses what it deduplicates) and refuses to run against an older one. The repository is created automatically on first use (`init --repository-version 2`) with the stored backup key as its password. Each component is one tagged snapshot (`ki`, `backup:<id>`, `key:<component key>`) streamed over stdin; the manifest is stored the same way, so a backup is complete in the repository or not there at all. Deleting a backup `forget`s its snapshots; space is reclaimed by one `prune` per retention pass, not per deleted backup. restic commands time out after `KI_BACKUP_RESTIC_TIMEOUT_SECONDS` (default 86400 s).

Because restic declares `provides_encryption`, `prefers_uncompressed` and `deduplicates`, the appliance hands it plaintext, uncompressed input — sealed or gzipped input would share no chunks with the previous night and deduplication would silently stop working.

The on-disk layout at `local` and `s3` destinations:

```
<prefix>/ki-backup-20260728T020000Z/
  manifest.json          contents, checksums, appliance metadata — readable without the key
  SHA256SUMS             sha256sum -c compatible; covers the manifest too
  components/…           one file per store
```

## Verification

### Preflight

`GET /api/backup/preflight` (the Status panel, re-checked after every save and on demand) answers "would tonight's backup work", now:

- destination built and probed by actually writing, reading back and deleting a probe object;
- encryption state: key set or not, its fingerprint, and who performs encryption;
- every component resolved and probed — `pg_isready` per database, cluster health and repository mounts for OpenSearch, directory existence for file sets, `KI_CONNECTOR_CREDENTIAL_KEY` presence for secrets;
- staging directory, count of mid-pipeline documents, the last backup run;
- schedule health: whether the schedule is on **and whether any scheduler loop is actually alive**, from a heartbeat written on every tick. A schedule switched on with no live watcher is reported as a problem, because nothing in the configuration alone can tell "backups run nightly" from "backups are configured to run nightly on a machine where no loop runs".

Only `postgres/ki` and the destination are reported as problems; every other unready store is a warning. While backups are disabled, everything is reported as warnings rather than faults.

### Post-write verification

With `verify_after_write` on (the default), the run reads every component back from the destination after the manifest is written and checks, per component:

| Check | Proves |
| --- | --- |
| Stored byte count and stored SHA-256 match the manifest | The bytes at the destination are the bytes that were written — catches truncated uploads, bad disks, partial syncs. |
| Decrypts in full under the on-hand key, with the expected context | The key on hand opens this backup, and it is the claimed component of the claimed backup. |
| Plaintext byte count and plaintext SHA-256 match the manifest | What comes out is the dump that went in. |

The deep (decrypting) check is the default because only it answers "could we restore from this"; decrypted bytes are hashed and discarded, never written. The same verification is available on demand for any backup: per row in the console ("Check it", `POST /api/actions/backup-verify`, synchronous) or `ki backup-verify` (with `--shallow` for the checksum-only level).

A run is marked `completed` only when every step — including verification, when enabled — succeeded. A backup that was written but failed verification fails the run with the per-component problems in the error.

## Retention

Grandfather-father-son, configured under `backup.retention` and applied after each successful run when `prune_enabled` is on:

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| `daily` | 7 | 0–365 | Keep the newest backup of each of the last N distinct days that contain one. |
| `weekly` | 4 | 0–520 | …of each of the last N ISO weeks. |
| `monthly` | 6 | 0–120 | …months. |
| `yearly` | 2 | 0–50 | …years. |
| `min_keep` | 1 | 1–100 | The newest N backups are always kept, whatever the rules say. |
| `prune_enabled` | `true` | — | Apply the rules after each run. Off = decide only, delete nothing automatically. |

How pruning decides: every backup id at the destination is parsed back to its timestamp and sorted newest first. Each rule claims the newest backup in each of its most recent N periods; a backup is kept if any rule (or `min_keep`) claims it and deleted otherwise. Two absolute safety properties:

- Periods are counted as *distinct periods that contain a backup*, not calendar periods — an appliance switched off for three months still holds its weekly and monthly copies instead of ageing everything out.
- An id the build cannot parse is always kept (`unrecognized`): it is something a human put there or a newer version wrote, and neither is this appliance's to delete. The backup a run has just written is additionally protected by id, so a clock that went backwards cannot make a run delete its own output.

`POST /api/actions/backup-prune` defaults to `dry_run: true` and returns every decision with its reasons; the console shows the preview before offering the destructive call. On restic, deleting forgets snapshots and one `prune` per retention pass reclaims the space.

## Schedule

A daily wall-clock schedule under `backup.schedule`, not an interval:

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Off = backups run only when started manually. |
| `hour` / `minute` | 2 / 0 | Local wall time in `timezone`. |
| `timezone` | `UTC` | IANA name, validated at save time; an unknown name is refused rather than silently falling back. |
| `defer_while_active` | `true` | Wait while documents are mid-pipeline at the scheduled time. |
| `defer_limit_minutes` | 180 | 0–1440. After this, enqueue anyway — unforced, so the run fails with the reason recorded, visibly, instead of another silent night. |

The loop runs inside `ki serve`, ticking every `KI_BACKUP_SCHEDULE_SECONDS` seconds (default 60; `0` disables the thread entirely for deployments that drive backups externally). Due-ness is read from the run ledger: a backup is owed when the schedule has fired since the last *attempt*, so a missed night is picked up when the appliance comes back, and a failed backup does not re-fire in a loop. Daylight-saving transitions are handled by construction — a skipped 02:00 still yields exactly one occurrence for that date, a repeated one yields the first. Every tick writes a heartbeat row (pid, host, last decision); preflight reports the watcher dead when the heartbeat is older than 15 minutes.

## Restore

Staging and applying are separate operations behind one endpoint. Both run as a `restore` run on the same ledger, with the same one-at-a-time rule as backups (a backup and a restore cannot run concurrently).

### Refusal conditions

`enqueue_restore` evaluates the restore plan *before* reserving anything; a plan with blockers refuses the request with `409` while the appliance is still untouched. Blockers and warnings, all read from the manifest:

| Condition | Class | Effect |
| --- | --- | --- |
| No backup key set, or key fingerprint differs from the one the backup was encrypted under | Blocker | Refused. There is no recovery path from the wrong key. |
| `KI_CONNECTOR_CREDENTIAL_KEY` fingerprint differs from the one recorded in the manifest | Blocker | Refused — restoring anyway would leave every stored OAuth token undecryptable. Set the key from the backup's `secrets/environment` component first. |
| Connector key not set here, so it cannot be checked | Warning | Reported; not refused. |
| Dump taken at a different schema revision than this build's head | Warning | Restore and let the app migrate forward on start. Never restore a dump from a newer build into an older one. |
| Embedding signature differs from the current configuration | Warning | Restored vectors will not match the configured model; plan a rebuild. |
| Manifest `schema_version` newer than this build reads | Refused at manifest load | Restore with a build at least that new. |

Additionally, a manifest missing at the destination means the id is an incomplete backup and is never offered (`404`).

### Staging

Staging downloads every component (or a `--component` subset from the CLI), decrypts it, and re-checks both byte count and plaintext SHA-256 against the manifest as it lands — refusing any component whose sealed context does not name this backup and this component. It writes nothing outside its own directory (`KI_RESTORE_STAGE_DIR`, default `/data/restore/<backup-id>`; files `0600`, directory `0700`) and is safe on a live appliance at any time. This is the "Try it — changes nothing" action in the console, and it is how a firm establishes that its backups are restorable before it needs them to be. A component already staged that still hashes to the manifest's value is reused rather than transferred again.

A staged directory is the estate in plaintext, including `environment.json`. When a restore run applied anything, the staged copy is deleted at the end; a stage-only run keeps it for inspection.

### Applying, per store

Nothing is applied unless its toggle was set on the request. The console offers four toggles ("Put it back"), mapping to component kinds:

| Toggle | Components | Applied how |
| --- | --- | --- |
| `apply_databases` | `postgres/*` | `pg_restore --clean --if-exists --no-owner --no-privileges` over the live database. Not `--exit-on-error`: error lines are collected and classified, and a known-benign class (a dump written by a newer `pg_dump` `SET`ting a parameter the older server lacks) does not fail the store. Any serious error fails the run — half a restore must not read as success. Timeout `KI_BACKUP_PG_RESTORE_TIMEOUT_SECONDS` (default 21600 s). |
| `apply_search_index` | `opensearch/snapshot` | The snapshot repository volume is emptied, the archive extracted into it, the repository registered, existing indices with the same names closed, the snapshot restored with `wait_for_completion`, indices reopened. |
| `apply_files` | `files/*` | Archives extracted over their directories, with path-traversal-safe extraction (entries that would escape the target are refused). Only meaningful with the app, worker and watcher stopped. |
| `apply_volumes` | `volumes/keycloak`, `volumes/hatchet-config` | Through the restore agent (below). Applied last, one at a time, because each stops the container that owns the volume. |

### The restore agent

Keycloak's data volume holds an embedded database that is open and memory-mapped while Keycloak runs, and Hatchet's config is read at boot — both can only be replaced with their owning container stopped, and a process inside the stack cannot stop the stack it runs in. The `restore-agent` compose service closes that gap. It is a separate container that holds the Docker socket (mounted read-only) precisely so that the app container never does, and its API is deliberately narrow:

- Two operations: replace the contents of a *named* volume from a *named* archive (stopping and restarting the one container that owns it), and restart one *named* compose service. Volume and service names come from fixed tables in the agent, never from the request.
- Archives are accepted only from the restore staging directory, and are checked for path escapes before anything is stopped.
- Reachable only on the compose network, and only with the shared secret in `KI_RESTORE_AGENT_SECRET` (`Bearer` header). With no secret set, the agent refuses every request.
- Containers are resolved by compose label, stopped with a 60 s grace period and polled until actually down; the owning container is started again whatever happened to the extraction.

The appliance reaches it via `KI_RESTORE_AGENT_URL`. When the agent is unreachable, the restore plan marks the volume stores as not restorable from the console and points at `scripts/restore-backup.sh`, which replaces those volumes with the whole stack stopped.

### Service restarts after apply

`pg_restore --clean` drops and recreates objects under services that hold connection pools, which then fail with cached-plan and type-OID errors until restarted. A restore therefore ends by restarting exactly the services whose stores it replaced (derived per component: e.g. `postgres/ki` → app, watcher, worker; `postgres/hatchet` → hatchet, worker), through the restore agent, in dependency order. The one service the run cannot restart is the one it is executing inside (the worker under Hatchet); that one is named on the run — status stays `completed`, with a `RestartRequired` note carrying the exact `docker compose restart` command. A restart that could not be performed is a warning with instructions, never a failed restore.

Restoring `postgres/ki` replaces the run ledger itself, deleting the row tracking the restore in flight. The run then writes a fresh completed record into the restored ledger (`ledger_replaced: true`), so the firm ends up with a record, in the database it now has, that this database came from that backup at that moment.

### Restoring onto different hardware

A recovery onto fresh hardware starts with a drive mounted somewhere the new appliance has never heard of. The console's "Look somewhere else…" control (`GET /api/backup/restorable?path=…`) lists complete backups at an arbitrary folder and the restore request carries it as `source_path`. The override keeps the configured destination *kind* and swaps only the location, and the path is checked against the same fixed roots the folder picker browses (`/backups`, `/data`, `/mnt`, `/media`, `/srv`, `/var/backups`, plus the configured path) — an admin-only endpoint is still not a way to point the appliance at arbitrary host paths.

Order of operations for different hardware: set `KI_CONNECTOR_CREDENTIAL_KEY` (from the backup's `secrets/environment` component) and the backup key first — the restore refuses on a mismatched connector key and cannot decrypt without the backup key — then restore. The dump restores under whatever superuser the new environment has; schema migrations run forward on next start. See [Backup & restore](/operations/backups/) for the whole-stack script.

## Console controls and their endpoints

Every backup endpoint requires an administrator identity.

| Console control | Endpoint | Notes |
| --- | --- | --- |
| Master On/Off switch | `PUT /api/config` | Takes effect on click; commits the rest of the page's unsaved state with it. |
| Save configuration / Save these settings | `PUT /api/config` | The whole `AppConfig`, validated as one object. |
| Status panel / Re-check | `GET /api/backup/preflight` | Re-asked after every save and every 5 s while a run is in flight. |
| Back up now… | `POST /api/actions/backup` `{force}` | `202` with `{run_id, backup_id}`; `409` if one is in flight. When documents are mid-pipeline the button becomes "Start anyway", which sends `force: true`. |
| Destination folder picker | `GET /api/backup/folders?path=…`, `POST /api/backup/folders` | Lists browsable places with free space, and creates sub-folders. Writability is probed by writing. |
| Access key / Secret key / paste key fields | `POST /api/backup/secrets` `{name, value}` | `value` absent forgets the secret. Values are never returned; `GET /api/backup/secrets` reports set-state and fingerprint only. |
| Generate a key for me | `POST /api/backup/generate-key` | Returns the key once; the dismiss button is disabled until it has been copied. |
| Your backups list | `GET /api/backup/backups?limit=50` | Read from the manifests at the destination, not from a local index; refreshed when a run ends. |
| Check it | `POST /api/actions/backup-verify` `{backup_id}` | Synchronous deep verification. |
| Restore from this… | `POST /api/actions/backup-restore-plan` `{backup_id, source_path}` | Report only; blockers, warnings, per-store steps and restorability. |
| Try it — changes nothing | `POST /api/actions/restore` with all `apply_*` false | Stage-and-verify run. |
| Put it back… | `POST /api/actions/restore` with the chosen `apply_*` flags | Behind a per-backup confirmation. |
| Look somewhere else… | `GET /api/backup/restorable?path=…` | Backups at a folder that is not the configured destination. |
| Preview / Delete old copies… | `POST /api/actions/backup-prune` `{dry_run}` | `dry_run` defaults to true. |
| Recent backups and restores | `GET /api/runs` | Filtered to workflows `backup` and `restore`. |

Settings on the page and where they live in configuration: the destination choices ("A folder" / "Cloud storage" and "Only store what changed each night") map to `destination.kind` = `local` / `s3` / `restic` — choosing restic also turns `encrypt` off, because restic encrypts for itself; the schedule step maps to `schedule.*`; the Advanced panel maps to `sources.*` (with `extra_paths` as a comma-separated field), `retention.*`, `require_settled_pipeline`, `schedule.defer_limit_minutes` and `max_component_gb`. The deployment-secrets toggle is disabled in the console whenever encryption is not guaranteed, mirroring the validator that would otherwise reject the save.

## Environment variables

These are deployment layout, read from the environment rather than stored in configuration — the compose file already knows where it mounted things. Everything about *what* is captured, *where* it goes and *when* lives in the console.

| Variable | Default | Read by | Purpose |
| --- | --- | --- | --- |
| `KI_BACKUP_STAGING_DIR` | `/data/backup-staging` | capture | Per-run staging; needs room for the largest single component. |
| `KI_BACKUP_KEYCLOAK_PATH` | unset (compose: `/backup-sources/keycloak`) | capture | Read-only mount of Keycloak's data volume. Empty = component skipped with a warning. |
| `KI_BACKUP_HATCHET_CONFIG_PATH` | unset (compose: `/backup-sources/hatchet-config`) | capture | Read-only mount of Hatchet's config volume. Empty = skipped. |
| `KI_BACKUP_OPENSEARCH_REPO_CONTAINER_PATH` | unset (compose: `/mnt/snapshots`) | capture, restore | The snapshot repository path as OpenSearch knows it; must be in its `path.repo`. |
| `KI_BACKUP_OPENSEARCH_REPO_PATH` | unset (compose: `/backup-sources/opensearch-snapshots`) | capture, restore | The same volume as mounted in the app/worker, where the finished snapshot is read and where a restore unpacks. |
| `KI_BACKUP_OPENSEARCH_REPO_NAME` | `ki-backup` | capture, restore | Snapshot repository name. |
| `KI_BACKUP_HATCHET_DATABASE_URL` | unset (compose-provided) | capture, restore | Hatchet's database is a separate server; cannot be derived. Empty = component skipped. |
| `KI_BACKUP_LITELLM_DATABASE_URL` | derived from `KI_DATABASE_URL` | capture, restore | Override only if the database was moved off the primary server. |
| `KI_BACKUP_LANGFUSE_DATABASE_URL` | derived from `KI_DATABASE_URL` | capture, restore | As above. |
| `KI_BACKUP_PG_DUMP_TIMEOUT_SECONDS` | `21600` | capture | Per-database `pg_dump` timeout. |
| `KI_BACKUP_OPENSEARCH_TIMEOUT_SECONDS` | `10800` | capture | How long to wait for a snapshot to reach `SUCCESS`. |
| `KI_BACKUP_PG_RESTORE_TIMEOUT_SECONDS` | `21600` | restore | Per-database `pg_restore` timeout. |
| `KI_BACKUP_RESTIC_TIMEOUT_SECONDS` | `86400` | restic destination | Timeout on each restic invocation. |
| `KI_BACKUP_SCHEDULE_SECONDS` | `60` | scheduler | Tick interval of the schedule loop in `ki serve`; `0` disables the thread (minimum otherwise 5). |
| `KI_RESTORE_STAGE_DIR` | `/data/restore` | restore, agent | Where restores stage and verify; the agent accepts archives only from under it. |
| `KI_RESTORE_AGENT_URL` | unset (compose: `http://restore-agent:8100`) | restore | Where the appliance reaches the restore agent. Unset = volume stores and automatic restarts unavailable from the console. |
| `KI_RESTORE_AGENT_SECRET` | unset (required by compose) | restore, agent | Shared bearer secret. Unset on the agent = it refuses every request. |
| `KI_RESTORE_AGENT_DOCKER_SOCKET` | `/var/run/docker.sock` | agent | The Docker socket the agent talks to. |
| `KI_BACKUP_MOUNT` | `./runtime/backups` (compose) | compose only | Host path mounted at `/backups` in app and worker — point it at the NAS. |

`KI_BACKUP_ENCRYPTION_KEY` and `KI_BACKUP_S3_SECRET_ACCESS_KEY` are *not* read by anything; they exist only on the never-capture list of the secrets component, in case a deployment exports them for its own reasons.

## Failure modes and operational notes

- **Permissions preconditions.** The `backup-permissions` init container in the compose file runs once before the app: it makes the shared OpenSearch snapshot volume writable by both OpenSearch and the appliance (`chmod 1777`, `/tmp`-style) and hands ownership of Hatchet's `0600` root-owned config to the appliance's user. Without it, both stores show up only as skipped components in a backup that otherwise reports success — which is exactly what preflight exists to surface.
- **Timeouts** fail the affected component (and the run, if the component is required): `pg_dump` 6 h, OpenSearch snapshot 3 h, `pg_restore` 6 h, restic 24 h per invocation — all overridable per the table above.
- **A stranded run blocks everything.** Because one backup/restore may be active, a reservation whose worker died holds the slot until the run sweeper resolves it (or it is cancelled from the pipeline page). Dispatch failures are closed out immediately; restored ghost runs are released automatically at the next reservation.
- **Per-component size limit.** A component larger than `max_component_gb` (default 512) fails the run outright. It is a guard against a runaway archive, not a total; raise it deliberately for a genuinely large estate.
- **Skipped components are warnings, not silence.** Every optional store that cannot be captured is named in the manifest's warnings, on the run row, and in the console's run history. Only `postgres/ki` and the destination fail a run.
- **Staged data is plaintext.** Both the backup staging directory (during a run) and the restore staging directory (after a stage) hold stores of the appliance in the clear, owner-only. The backup path deletes per component after transfer and the whole directory at the end; a stage-only restore keeps its directory deliberately.
- **The `local` destination default (`./runtime/backups`) is on the same host.** It survives `docker compose down -v` but not the machine; it is for trying the feature, not for protection. Point `KI_BACKUP_MOUNT` at storage the appliance's failure cannot take with it.
- **Listing is destination truth.** The backups list is read from the manifests at the destination on every request, never from a local index that could disagree with reality. An id without a readable manifest is listed as incomplete and offered no verify or restore actions.
- **Verification doubles the transfer.** `verify_after_write` reads the whole backup back every night. On a metered S3 endpoint this is a real cost; it is also the only thing that distinguishes a backup that exists from one that is readable.
