# Hosting the demo at legalmemory.eigenweltlabs.com

One GCE VM running the demo stack behind Caddy. The docs are served from the
same origin under `/docs`, so there is one certificate and one DNS record.

## Why a VM and not Cloud Run

The stack includes OpenSearch, which wants a persistent data directory, a fixed
heap and a memlock ulimit. That is a poor fit for a request-scaled container
runtime and a good fit for a machine. The rest of the stack is already compose,
so it comes along.

## Sizing

Measured from a 13,544-document index, then scaled to 50,000:

| | 13.5k (measured) | 50k (projected) |
|---|---|---|
| Postgres | 9.1 GB | ~34 GB |
| OpenSearch | 4.9 GB | ~18 GB |
| Artifacts | 0.7 GB | ~2.5 GB |

`n2-standard-8` (8 vCPU, 32 GB) with a **200 GB** balanced persistent disk. The
memory split is deliberate: 8 GB to Postgres shared buffers, 4 GB to the
OpenSearch heap, the rest left to the page cache, which is what actually makes
the 34 GB database fast.

## One-time provisioning

```bash
PROJECT=…  ZONE=europe-west3-a  INSTANCE=legalmemory-demo

gcloud compute instances create "$INSTANCE" --project="$PROJECT" --zone="$ZONE" \
  --machine-type=n2-standard-8 \
  --boot-disk-size=200GB --boot-disk-type=pd-balanced \
  --image-family=cos-stable --image-project=cos-cloud \
  --scopes=https://www.googleapis.com/auth/cloud-platform \
  --tags=https-server,http-server

# The edge needs 80 and 443; nothing else is published, and every other port in
# the compose files binds to 127.0.0.1.
gcloud compute firewall-rules create legalmemory-demo-web --project="$PROJECT" \
  --allow=tcp:80,tcp:443 --target-tags=https-server,http-server

# Deploys and shell access run over IAP, so the VM needs no public SSH port.
gcloud compute firewall-rules create legalmemory-demo-iap --project="$PROJECT" \
  --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=https-server
```

Point an `A` record for `legalmemory.eigenweltlabs.com` at the instance's static
IP **before** the first deploy — Caddy obtains its certificate over ACME on
first start and needs the name to resolve.

## Secrets

The workflow reads one Secret Manager secret, `legalmemory-demo-env`, and writes
it to `~/.env.demo` on the instance. It is the same file as `.env.demo.example`
plus `DEMO_DOMAIN` and `ACME_EMAIL`. Nothing is stored in the repository and
nothing is echoed into a workflow log.

```bash
gcloud secrets create legalmemory-demo-env --data-file=.env.demo --project="$PROJECT"
# later
gcloud secrets versions add legalmemory-demo-env --data-file=.env.demo --project="$PROJECT"
```

## Repository configuration

Variables: `GCP_PROJECT`, `GCP_REGION`, `GCE_INSTANCE`, `GCE_ZONE`, `DEMO_DOMAIN`.
Secrets: `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA` — Workload Identity Federation, so
no service-account key is stored.

## Deploying

`Actions → Deploy demo → Run workflow`. Manual on purpose: this is a demo with a
real index behind a real domain, and a redeploy on every push to main is how it
becomes unavailable during the meeting it was built for.

The workflow builds the appliance and demo images, pushes them to Artifact
Registry, ships the compose files and the built docs to the instance, restarts
the stack, optionally migrates, and then checks that `/`, `/docs/` and `/mcp/`
answer before reporting success.

## Loading the index

The stack starts empty. A corpus lives in two stores, and only one of them
travels in a database dump:

| Store | Holds | Travels as |
|---|---|---|
| Postgres | documents, chunks, metadata, **and the embedding vector per chunk** | `pg_dump --format=custom` |
| OpenSearch | a copy of each chunk's text and vector — what queries actually hit | its own snapshot, or a rebuild |

So restoring a dump leaves search returning nothing until the index is
populated, by one of two routes:

* **Restore an OpenSearch snapshot** of the source index, if you have one and the
  clusters are the same major version. It is a file copy — minutes.
* **Rebuild from `chunks.embedding`** with `deploy/reindex_from_embeddings.py`.
  No re-embedding and no token spend, since the dump already carries the vectors
  and `OpenSearchIndex.bulk_sync` reads them rather than recomputing. Budget
  roughly an hour per few million chunks on an 8-vCPU host.

### Check the embeddings first — the index name will not warn you

The index name is derived from the embedding *signature*, which keys on vector
**width**, not on which model produced the vectors. Two different models at the
same width therefore resolve to the same index name. Load a corpus embedded by
model A into a deployment that embeds queries with model B and every step
succeeds — restore, reindex, healthchecks green — while dense retrieval compares
query vectors against document vectors from a different model and returns
confident nonsense.

Two things must line up, and both are silent when they do not:

1. **The model.** `KI_EMBEDDING_MODEL` / `KI_EMBEDDING_UPSTREAM` must name the
   model the corpus was embedded with.
2. **The width.** Some models emit wider vectors than the index expects and must
   be truncated back — `dimensions:` in the embedding block of
   `deploy/litellm/demo-config.yaml`, which LiteLLM maps onto the provider's own
   parameter (Gemini's `output_dimensionality`, for instance). There is no
   environment variable for it, so copying `KI_EMBEDDING_*` across from wherever
   the corpus was built is *not* sufficient.

Ask the provider how wide its vectors actually are before loading anything, and
compare against the width the index was built at:

```bash
docker compose exec -T postgres psql -U ki -d ki -Atc \
  "SELECT vector_dims(embedding) FROM chunks WHERE embedding IS NOT NULL LIMIT 1;"
```

Also confirm `alembic_version` in the dump matches the appliance you are loading
it into, and — if the corpus came from a deployment with connectors configured —
that `source_credentials` is empty, since those rows are encrypted with the
*source's* `KI_CONNECTOR_CREDENTIAL_KEY` and are undecryptable anywhere else.

### Replacing a corpus that is already live

Back up first. Both halves can be captured without stopping anything: `pg_dump`
takes an MVCC-consistent snapshot, and the OpenSearch snapshot API does not need
the node quiesced. Verify what you wrote rather than trusting it — a session
killed mid-archive leaves a plausible file of roughly the right size — and keep
the OpenSearch snapshot, because it makes a rollback a restore rather than
another rebuild.

Then keep the outage down to the index step:

* Restore the incoming dump into a **side database** (`ki_new`) while the current
  one keeps serving, then cut over with two `ALTER DATABASE ... RENAME`s. Renaming
  the old database aside rather than dropping it is what makes the rollback cheap.
* Stage the new index as a snapshot ahead of time, so the cutover restores it
  instead of rebuilding it.
* Note that `docker cp`-ing a large dump into the postgres container needs it on
  disk twice; bind-mount the staging directory into a throwaway client container
  on the compose network and restore over TCP instead.

Restoring a snapshot still needs the target index name free, which is the one
irreducible piece of downtime. To remove it, restore into a differently named
index and have the app's index name be an **alias** pointing at it: an alias flip
is atomic, and the cost is only the disk to hold both indexes while you switch.

Afterwards, free the staged snapshot. OpenSearch stops allocating shards at 90%
disk and turns indices read-only at 95%, and two corpora plus a backup plus a
staged snapshot will get there faster than expected.
