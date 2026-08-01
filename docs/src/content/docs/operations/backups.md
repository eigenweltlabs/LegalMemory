---
title: Backup & restore
description: The full reference — what a backup contains, encryption and verification, destinations, retention, and the staged restore.
---

This appliance holds a law firm's document estate, the permission model that decides who
may see which parts of it, an append-only access ledger the firm may have to produce, and
the OAuth credentials that keep all of it synced. Losing it is not an outage. This page is
how it gets backed up, and — the part that actually matters — how it comes back.

Everything here is configured under **Backup** in the admin UI. There is nothing to edit
in a config file.

## What is in a backup

A backup is the whole appliance, not a database. That distinction is the design: restoring
a Postgres dump without the search index, the uploaded files and the connector credential
key produces something that starts, answers questions, and is quietly missing a third of
the estate — which is worse than a restore that refuses to run.

| Component | What it holds | If it were missing |
|---|---|---|
| `postgres/ki` | The ontology, matters, documents, ACL grants, audit ledger, encrypted connector credentials | Everything. Not optional; a backup without it fails |
| `postgres/litellm` | Gateway spend ledger and runtime model registry | Cost history, and any model added from the admin UI |
| `postgres/langfuse` | Every model-call trace | The audit record of what the models were shown and returned |
| `postgres/hatchet` | Workflow definitions, run history, the durable queue | In-flight work and the run history behind it |
| `opensearch/snapshot` | The chunk index, its mappings and analyzers | Rebuildable by re-embedding every chunk — hours, and real money |
| `files/artifact-blobs` | Content-addressed originals of every fetched document | Re-fetchable only while each source still exists and still holds the file |
| `files/uploaded` | Files imported through the admin UI | Outright. There is no upstream copy anywhere |
| `files/connector-staging` | Mid-sync scratch (off by default) | Nothing a restore can use; a restore starts no scan |
| `volumes/keycloak` | Users, sessions, client secrets, realm signing keys | Every login, and the keys that make existing tokens valid |
| `volumes/hatchet-config` | Hatchet's generated server config | The client token stops being valid; re-mint with `scripts/bootstrap-hatchet.sh` |
| `files/watched` | The folders the watcher was asked to keep an eye on | The watcher sits idle over an estate nobody notices has stopped being indexed |
| `files/appliance-config` | The top-level files of the data directory — `config.json`, the whole of the appliance's configuration | The estate comes back to an appliance that has forgotten how it was set up |
| `secrets/environment` | The deployment's `KI_*` secrets, above all `KI_CONNECTOR_CREDENTIAL_KEY` | See below — this is the one that ruins recoveries quietly |

### The connector credential key

Connector OAuth tokens are stored as AES-256-GCM ciphertext under
`KI_CONNECTOR_CREDENTIAL_KEY`, which lives in the deployment's environment and not in the
database. Restore the database under a *different* key and everything appears to work —
until the first token refresh, at which point every connector the firm has authorized is
dead and nothing says why.

So the key is captured in the backup, the manifest records its fingerprint, and a restore
refuses to proceed when the fingerprint does not match the key the deployment currently
holds. That refusal is the feature.

Because the backup therefore contains the keys to the firm's document estate,
`sources.environment_secrets` cannot be turned on unless `encrypt` is on. The
configuration will not validate otherwise — it is not a warning you can click past.

## Encryption

On by default. AES-256-GCM under a 32-byte key, the same primitive and key-handling
convention as the connector credential key. The appliance generates the key itself —
there is no shell command to run first.

Set it under Backup → Security in the admin UI: press **Generate** and the appliance makes
one, shows it once, and stores it encrypted in its own database. **Keep the copy it shows
you somewhere that is not this appliance.** `ki backup-key --generate` does the same thing
for a deployment with no browser. There is no environment variable for it: a secret that
can be set two ways is a secret nobody can say the current value of. **Keep the shown
copy somewhere that survives this appliance** — a backup whose key exists only on the
machine the backup protects is not a backup. The key is deliberately never captured into the
backups it protects.

Each component is sealed as a sequence of chunks rather than one message, and the framing
authenticates each chunk against its own position and against whether it is the last one.
A backup cut short by a full disk or a killed transfer therefore fails to decrypt, instead
of opening into a shorter, plausible-looking dump that restores without complaint.

The manifest itself is *not* encrypted. During a disaster an operator has to be able to
see what a backup holds, which appliance it came from and whether they hold the right key,
before they can decrypt anything. It carries only names, sizes, digests and fingerprints,
and its own digest is in `SHA256SUMS`.

## Where backups go

Three destinations, chosen under Backup → Destination.

**`local`** is a directory, which in practice is a mounted NAS or SMB share. Point
`KI_BACKUP_MOUNT` at it in `.env`; it is mounted into the app and worker at `/backups`.
The default is `./runtime/backups` on the host, which survives `docker compose down -v`
but is on the same machine — fine for trying the feature out, not a backup.

**`s3`** is any S3-compatible endpoint: MinIO on the firm's own hardware, Wasabi, AWS.
Credentials are typed under Backup → Destination and held encrypted in the database, never
in `config.json`. It needs boto3, which is
an optional extra so an air-gapped install never has to ship an AWS SDK:

```bash
pip install 'knowledge-index[s3]'
```

**`restic`** is a restic repository, and it is the one to choose at any real scale. The
other two store whole objects: a night's dump lands beside last night's and shares nothing
with it, so a hundred-thousand-document estate is transferred and stored again every
night, times whatever retention keeps — nineteen full copies under the default rules.
restic stores content-defined chunks instead, so a night that changed a few thousand rows
costs the difference rather than the estate, and a night that changed nothing costs
nothing. Measured on this appliance's own test suite: a second backup of an unchanged
estate adds under half the first one's bytes, and in a realistic 60 MB case a 0.05% change
cost 8.5 MB against a 60 MB full copy.

Two things follow automatically and are worth knowing rather than discovering:

* **restic does the encryption**, so `backup.encrypt` stays off and the appliance hands it
  plaintext. That is deliberate, not a gap. Sealing a component first produces a stream
  that shares no chunks with anything, which is exactly the property deduplication needs
  and encryption destroys — the backups would still be encrypted and the feature paid for
  would silently stop working. The repository password is the same backup key the other
  destinations use, so there is one backup secret to keep safe rather than two.
* **archives and dumps are written uncompressed** for the same reason: gzip turns a small
  change into a completely different byte stream. restic compresses what it stores after
  chunking, so nothing is lost — the compression just happens where it does not defeat the
  chunker. This needs restic 0.14 or later, and the destination refuses to run against an
  older one rather than quietly storing everything raw.

Between them these cover the "two media, one off-site" half of the 3-2-1 rule. Use both if
the firm can: run the schedule to the NAS and periodically copy to the bucket, or point a
second appliance's `ki backup-verify` at the same destination.

The layout at the `local` and `s3` destinations is the same, and is plain enough to work
with by hand — a restic repository is restic's own format and is read with `restic`:

```
<prefix>/ki-backup-20260728T020000Z/
  manifest.json          what this backup contains — readable without the key
  SHA256SUMS             sha256sum -c compatible, covers the manifest too
  components/…           one file per store
```

## Verification

`verify_after_write` is on by default: after writing, every component is read back from
the destination, its stored checksum re-checked, decrypted, and its plaintext checksum
re-checked. It doubles the transfer, and it is the only thing that distinguishes a backup
that exists from a backup that is readable. The modern form of the rule is 3-2-1-1-0, and
the zero is "zero unverified restores".

A backup that fails verification fails the run. It is not reported as a success with a
note.

Verify an older backup at any time, from the UI or:

```bash
docker compose exec app ki backup-verify ki-backup-20260728T020000Z
```

`--shallow` checks the stored checksums without decrypting: faster, and proves less. The
default is the one that answers "could we restore from this".

## Schedule and retention

The schedule is a wall-clock time in the firm's own timezone, not an interval — an
interval drifts into the working day the first time a run is slow. Set the hour, the
minute and an IANA timezone under Backup → Schedule; "every night at two" then means two
o'clock where the firm is, in January and in July, instead of moving an hour against them
twice a year. Due-ness is read from the run ledger, so an appliance that was off over the
weekend takes its backup when it comes back, and a backup that failed is not retried every
minute of the night. The two awkward days are handled by construction: a local time that
daylight saving skipped still yields exactly one backup for that date, and one that
happened twice does not yield two.

**A schedule only fires if something is watching the clock.** The loop runs inside
`ki serve`, and `KI_BACKUP_SCHEDULE_SECONDS=0` leaves it out. Nothing in the configuration
can tell "backups run nightly" from "backups are configured to run nightly on a machine
where no loop is running", so the scheduler records a heartbeat on every tick and preflight
reports it: a schedule switched on with no live watcher is a problem on the status panel,
not a silence.

If documents are still mid-pipeline at the scheduled time the run waits, because a backup
taken then holds a database that knows about files the artifact archive has not finished
writing. It waits up to `defer_limit_minutes` and then runs anyway — unforced, so it fails
with the reason recorded on it. A red backup in the run list is the right outcome; another
silent night is not.

Retention is grandfather-father-son: the newest of each of the last N days, weeks, months
and years. On a restic destination, forgetting a backup and reclaiming its space are
separate steps — a retention pass forgets each backup it is dropping and then prunes once,
because pruning rewrites pack files and doing that per backup costs many times more. Counting periods that contain a backup rather than calendar
periods means a stack that sat idle for three months still holds a weekly and a monthly
copy. Two rules are absolute: `min_keep` newest backups are never pruned, and a directory
whose name this build cannot parse is never deleted — an unrecognized entry is something a
human put there.

Preview before you enable pruning:

```bash
docker compose exec app ki backup-prune          # reports only
docker compose exec app ki backup-prune --apply  # deletes
```

## Preflight

The way this feature fails is not a crash. It is running nightly for eight months against
a share that was unmounted in March. The Status panel at the top of the Backup page, and
`ki backup-preflight`, exist to make that discoverable on a Tuesday afternoon instead of
during a recovery: destination reachable and writable, encryption key present and its
fingerprint, every component resolved to a real path, free space, and when the last backup
ran.

```bash
docker compose exec app ki backup-preflight
```

Exits non-zero if anything is wrong, so it is worth putting in whatever the firm uses for
monitoring.

## Restoring

Staging and applying are separate, deliberately.

**Staging is always safe.** It downloads, decrypts and re-checks every component against
the manifest, and touches nothing else. It works on a live appliance. This is how a firm
turns "we have backups" into "we have restorable backups", and it is worth doing on a
schedule of its own:

```bash
docker compose exec app ki backup-restore ki-backup-20260728T020000Z --stage-to /tmp/check
```

Staging writes the backup back out as plaintext — the database dumps of the whole estate,
and `environment.json` with `KI_CONNECTOR_CREDENTIAL_KEY` in it. The staged files and the
directory holding them are created owner-only, but that is the floor and not the whole
answer: stage somewhere the firm is willing to have those bytes, and delete it afterwards.

**Applying is the destructive half.** Each store is a separate flag and all of them
require the long confirmation flag:

```bash
docker compose exec app ki backup-restore ki-backup-20260728T020000Z \
  --stage-to /restore --apply-databases \
  --i-understand-this-destroys-current-data
```

`--reuse-staged` keeps components already in `--stage-to` that still hash to what the
manifest says, instead of transferring and decrypting them again. Nothing is applied
without having been checked against the manifest in that same call; the flag only avoids
paying for the transfer twice when staging and applying are separate invocations, which is
what `scripts/restore-backup.sh` does.

A restore that reports errors exits non-zero and names them. `pg_restore` is deliberately
not run with `--exit-on-error`, so a store can come back with errors and still be reported;
the ones that cannot have cost data — a dump written by a newer `pg_dump` than the target
server opens by setting a parameter the server does not know — are not counted against it.
Anything else means the appliance now holds part of the backup and part of what was there
before. Do not start it and call it recovered.

Restoring the file stores means the app, worker and watcher must be stopped first —
extracting the blob store underneath a running pipeline hands it files it has already
decided are missing. The search index does not need that: the restore closes the affected
indices, restores the snapshot, and reopens them.

**For a whole-appliance restore, use the script.** Keycloak's data volume and Hatchet's
config volume can only be replaced with their containers stopped. A restore started from
the admin UI can do that too, through the `restore-agent` service — the one container that
holds the Docker socket — but when the agent is not running, the script is the only path,
because a process inside the stack cannot stop the stack it is running in:

```bash
scripts/restore-backup.sh ki-backup-20260728T020000Z --yes
```

It checks the plan for blockers before it stops anything, so a restore that cannot work
costs no downtime; stages and verifies the whole backup before it destroys anything;
replaces the two volumes; restores the databases, index and files; and starts the stack.

Afterwards, two things still need a human: re-minting the Hatchet client token if it was
rotated since the backup, and placing any deployment secrets from
`environment.json` that differ from this deployment's `.env`.

### Restoring onto a different appliance

Set `KI_CONNECTOR_CREDENTIAL_KEY` from the backup's `secrets/environment` component
*before* restoring, or the restore will refuse — which is what you want. A schema revision
older than the current build is fine: restore it and let the app migrate forward on start.
The reverse never is; do not restore a dump from a newer build into an older one.

If the embedding model has changed since the backup, the restored vectors will not match
the configured model. `restore_plan` reports it as a warning and the fix is a rebuild from
the Models page, not a different restore.

## What is not covered

- **Anything outside the appliance.** The read-only mount of the firm's own filesystem is
  the firm's to back up; this appliance holds a shadow index of it, not the estate.
- **`deploy/keycloak/tls/`** if the firm uses a real certificate rather than the generated
  self-signed one. It is not in a volume and not in git.
- **Point-in-time recovery.** Every backup is a full backup taken at an instant. A firm
  that needs to recover to an arbitrary moment wants WAL archiving on the Postgres
  container in addition to this, not instead of it.

## Environment reference

Set in `docker-compose.yml`; override in `.env`. These are deployment layout, which is why
they are variables rather than settings — what is captured, where it is written and when
all live in the admin UI.

| Variable | Default | Purpose |
|---|---|---|
| `KI_BACKUP_MOUNT` | `./runtime/backups` | Host path mounted at `/backups` — point at the NAS |
| `KI_BACKUP_STAGING_DIR` | `/data/backup-staging` | Needs room for the largest single component |
| `KI_BACKUP_HATCHET_DATABASE_URL` | compose-provided | Hatchet's database is a different server, so it cannot be derived |
| `KI_BACKUP_LITELLM_DATABASE_URL` | derived | Override only if it was moved off the primary server |
| `KI_BACKUP_LANGFUSE_DATABASE_URL` | derived | As above |
| `KI_BACKUP_OPENSEARCH_REPO_CONTAINER_PATH` | `/mnt/snapshots` | Must match OpenSearch's `path.repo` |
| `KI_BACKUP_OPENSEARCH_REPO_PATH` | `/backup-sources/opensearch-snapshots` | The same volume, as mounted in the app |
| `KI_BACKUP_KEYCLOAK_PATH` | `/backup-sources/keycloak` | Read-only mount of Keycloak's volume |
| `KI_BACKUP_HATCHET_CONFIG_PATH` | `/backup-sources/hatchet-config` | Read-only mount of Hatchet's config volume |
| `KI_BACKUP_SCHEDULE_SECONDS` | `60` | How often the loop checks the clock; `0` leaves the thread out |
| `KI_BACKUP_PG_DUMP_TIMEOUT_SECONDS` | `21600` | Raise for a genuinely enormous database |
| `KI_BACKUP_OPENSEARCH_TIMEOUT_SECONDS` | `10800` | How long to wait for a snapshot to finish |

## Relationship to the insertion snapshots

`scripts/snapshot-insertion.sh` is a different tool for a different job: it freezes a
completed insertion so model-comparison runs can be replayed against identical state, and
it deliberately leaves LiteLLM, Langfuse and Keycloak alone. It also requires the stack to
be stopped. Keep using it for benchmarking. It is not a backup, and this is.
