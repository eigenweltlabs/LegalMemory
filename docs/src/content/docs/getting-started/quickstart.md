---
title: Quick start
description: Bring the appliance up with Docker Compose, configure the required environment, and index a first source.
---

This walkthrough brings the full stack up on one machine and indexes a first
source. Nothing is mocked — every step runs the same services and models a
production deployment does, so indexing costs real (small) model spend from
the first document.

## Prerequisites

- Docker with Compose
- Python 3.11+ (3.13 recommended; used for the `ki` CLI)
- An API key for at least one model provider (OpenAI works out of the box)

## 1. Configure the environment

All required configuration is declared in `docker-compose.yml` with `${VAR:?}`,
so a missing value stops the stack with the name of what is missing instead of
starting an appliance that fails on its first model call.

```bash
cp .env.example .env
```

Then fill in `.env`. The required values are:

| Variable | What it is |
| --- | --- |
| `KI_OPENAI_API_KEY` | Provider key for the model gateway. `KI_`-prefixed on purpose: a stale `OPENAI_API_KEY` exported in your shell would silently shadow a plain name. |
| `KI_SCW_SECRET_KEY` | Second provider key (Scaleway). Set a placeholder if you only route through OpenAI. |
| `KI_LLM_MODEL` / `KI_LLM_UPSTREAM` | The LLM's name and the provider route it resolves to (e.g. `gpt-5-mini` / `openai/gpt-5-mini`). No model name ships in any config file — the deployment decides. |
| `KI_LLM_INPUT_COST_PER_TOKEN` / `KI_LLM_OUTPUT_COST_PER_TOKEN` | Contracted per-token USD rates, so the cost centre never guesses. |
| `KI_EMBEDDING_MODEL` / `KI_EMBEDDING_UPSTREAM` / `KI_EMBEDDING_INPUT_COST_PER_TOKEN` | Same three answers for the embedding model. |
| `KI_CONNECTOR_CREDENTIAL_KEY` | Base64 32-byte AES key encrypting every stored connector credential. Generate with `openssl rand -base64 32`. Losing it means re-authorizing every connector. |
| `KI_RESTORE_AGENT_SECRET` | Shared secret protecting the restore helper that can replace container volumes. Generate the same way; never ship the placeholder. |

Optional but worth knowing on day one:

- `KI_DOCS_URL` — where this documentation is deployed; the admin UI links to
  it from the sidebar and the connector setup panels.
- `KI_PUBLIC_BASE_URL` — the appliance's public URL. The OAuth redirect URI for
  every connector is derived from it, so set it before connecting cloud
  sources. Defaults to `http://localhost:8000`.
- `KI_LOCAL_MOUNT` — which host directory the local-folder connector may see
  (mounted read-only into the containers).
- `KI_BACKUP_MOUNT` — where full backups are written. Point it at a NAS or
  external disk; the default stays on the same machine.

## 2. Bring the stack up

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # the `ki` CLI
docker compose up -d --build
bash scripts/bootstrap-hatchet.sh   # one-time: mints the orchestrator worker
                                    # token, writes it to .env, restarts app+worker
```

Check health: `curl localhost:8000/healthz`, the Hatchet UI on
`localhost:8888`, OpenSearch on `localhost:9200/_cluster/health`.

## 3. Index a first source

The fastest first source is a local folder: the host filesystem is mounted
read-only into the containers (scope it with `KI_LOCAL_MOUNT` in `.env`).
Either add it from the console (**Connectors → Files from this computer**) or
from the CLI:

```bash
docker compose exec app ki add-source /path/to/documents --name "First estate"
docker compose exec app ki sync
```

For a cloud source instead (SharePoint Online, OneDrive, Google Drive, Clio),
register the provider app first — each [connector guide](/connectors/) has the
exact steps — then connect from the console.

A sync that finds new documents starts the insertion pipeline by itself
(`pipeline.auto_insert_after_sync`, on by default). Watch progress on the
[Insertion pipeline](/product/pipeline/) page of the console or in the Hatchet
UI on `localhost:8888`. To trigger a re-pass manually:

```bash
curl -X POST localhost:8000/api/actions/pipeline \
  -H "x-ki-principals: user:local-admin,role:admin"
```

## 4. Open the admin console

- `http://localhost:8090` — the production-style entry through oauth2-proxy and
  Keycloak. The development realm seeds `admin@example.com` /
  `Legalmemory1-dev` and a non-admin `ma.associate` account.
- `http://localhost:8000` — the direct development port. It accepts a
  development identity header; the sign-in gate offers it under "Local
  development access". Never expose this port beyond the local machine.

First-run configuration happens in the console:

1. **[Models & services](/product/models-and-services/)** — confirm the stage
   model assignments and the embedding model resolve to the models you
   configured; add more via gateway credentials if needed.
2. **[Connectors](/product/connectors/)** — connect a real source. Cloud
   connectors need an app registration in the provider's console; each
   [connector guide](/connectors/) walks through it.
3. **[Sign-in](/product/sign-in/)** — point the appliance at your identity
   provider so colleagues sign in with their work accounts.
4. **[Access control](/product/access-control/)** — verify who can see what
   before opening the index to the firm.
5. **[Backup](/product/backup/)** — configure a destination and schedule
   before the index holds anything you would miss.

## Where next

- [Connecting a source](/connectors/) — the concepts every connector shares.
- [Deployment & identity](/operations/deployment/) — production checklist,
  OIDC/MCP sign-in, user provisioning.
- [External access](/product/external-access/) — point Claude or another MCP
  client at `https://<appliance>/mcp/`.
