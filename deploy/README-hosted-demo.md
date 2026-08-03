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

The stack starts empty. To load a corpus, restore a dump into the `postgres`
service and rebuild the search index from the embeddings the dump already
carries — no re-embedding required, since `chunks.embedding` is populated and
`OpenSearchIndex.bulk_sync` reads it rather than recomputing.
