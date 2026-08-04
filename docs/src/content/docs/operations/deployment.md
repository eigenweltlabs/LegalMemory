---
title: Deployment & identity
description: The service stack, identity chain, MCP sign-in, user provisioning, production checklist, and how syncing behaves in operation.
---

## What the stack is

Every capability is a real service; there are no offline substitutes,
fallbacks, or demo stand-ins anywhere in the pipeline. A stage whose dependency
is down fails, retries with backoff, and quarantines; it never silently
degrades.

| Service | Role | Default |
|---|---|---|
| `app` / `worker` | ontology, permission compiler, pipeline stages, MCP, admin UI | port 8000 (app) |
| `postgres` (pgvector) | knowledge layer, ACLs, pipeline state | host 5439, db `ki` |
| `opensearch` | ACL-scoped lexical + vector retrieval | host 9200 |
| `litellm` | model gateway for every LLM/embedding call | host 4000 |
| `docling` | document conversion + OCR (de/en) | host 5001 |
| `hatchet` + `hatchet-postgres` | pipeline orchestration (default provider) | UI on 8888 |
| `keycloak` + `oauth2-proxy` | identity (seeded realm with dev users) | 8083 / 8090 |

Every model call resolves through the gateway; each pipeline stage carries its
own model assignment, swappable in the admin UI, including to vLLM/TEI
endpoints for air-gapped installs. Which models a deployment runs is
set in `.env`; see the [Quick start](/getting-started/quickstart/).

For bring-up and first-source setup, see the
[Quick start](/getting-started/quickstart/). This page covers what an operator
needs beyond it.

## Identity

The production entry point is `http://localhost:8090` (oauth2-proxy → Keycloak
→ app). The development realm seeds `admin@example.com` / `Legalmemory1-dev`
and a non-admin `ma.associate` account. Keycloak issues tokens with a `groups`
claim, oauth2-proxy forwards user + groups upstream, and the app maps
membership of the configured admin groups (default `knowledge-index-admins`)
to `role:admin`.

`trusted_header` mode on the direct port 8000 additionally accepts
`X-KI-Principals`, a development convenience for the REST API and admin UI:
anyone who can reach 8000 can claim any identity, so never expose 8000 (or
arbitrary header forwarding) beyond the local machine. The MCP endpoint does
not honour it (see below).

## Signing in from an MCP client

A lawyer adds `https://<appliance>/mcp/` to Claude Desktop, Claude Code, or
MCP Inspector, signs in with their normal work account, and from then on
searches exactly the documents the source system says they may see. Nothing is
pasted and no token is minted by hand.

The appliance is an OAuth 2.1 **resource server** (MCP spec 2025-06-18); the
firm's identity provider is the **authorization server**. It never sees a
password and never issues a token. Three endpoints carry the handshake:

| Endpoint | Purpose |
| --- | --- |
| `POST /mcp/` without a token | `401` + `WWW-Authenticate: Bearer resource_metadata="…"` (RFC 6750 §3.1), which makes a client start a login instead of reporting a connection error. |
| `GET /.well-known/oauth-protected-resource/mcp` | RFC 9728 metadata: the resource identifier, the authorization server, and the scopes to ask for. Unauthenticated by necessity. |
| `GET /.well-known/oauth-authorization-server` | `307` to the identity provider's own metadata, for clients that predate RFC 9728. |

The client then reads the IdP's metadata, registers itself (dynamic client
registration) or uses a pre-registered client id, runs authorization code +
PKCE in the browser, and calls `/mcp/` with the resulting bearer token.

**Audience binding.** A token is accepted only if its `aud` contains this
appliance's resource identifier, by default `KI_CONNECTORS__PUBLIC_BASE_URL`
+ `/mcp`, overridable with `KI_SECURITY__MCP_RESOURCE`. A token the same
identity provider minted for a different application in the firm is refused,
even though the signature and issuer are valid. No identity provider in wide
use implements the RFC 8707 `resource` parameter yet (Keycloak 26 ignores it),
so the audience has to come from a scope whose mapper writes the resource
identifier into `aud`; the shipped realm defines that scope as
`knowledge-index-mcp` and advertises it in `scopes_supported` so
self-registering clients ask for it.

**The `x-ki-principals` escape hatch does not work on `/mcp`** regardless of
the global `auth_mode`. To use it while developing, set
`KI_MCP_DEV_TRUSTED_HEADER=true` (config key
`security.mcp_allow_trusted_header`). It is off by default and belongs on no
firm's appliance: with it on, anything that reaches port 8000 names itself in
a header and becomes any lawyer in the firm.

### Pointing the appliance at the firm's own identity provider

Four settings, all `KI_`-prefixed environment variables or fields under
`security` in the config:

| Setting | Meaning |
| --- | --- |
| `KI_SECURITY__OIDC_ISSUER` | The issuer identifier that must appear in a token's `iss`, and what is advertised as the authorization server. Must be the URL the **lawyer's laptop** can reach. |
| `KI_SECURITY__OIDC_JWKS_URL` | Where this appliance fetches signing keys. Only needed when the IdP answers on a different name inside the network than outside, exactly the case in the shipped compose file. Empty derives it from the issuer. |
| `KI_SECURITY__MCP_RESOURCE` | The resource identifier. Empty derives it from `KI_CONNECTORS__PUBLIC_BASE_URL` + `/mcp`. |
| `KI_SECURITY__MCP_SCOPES` | What clients are told to request. The entry that carries the audience mapper must be in this list. |

On the identity provider, three things are needed:

1. **An audience mapper** that puts the resource identifier into the access
   token's `aud`. In Keycloak: *Client scopes → create `knowledge-index-mcp` →
   Mappers → Add → Audience → Included Custom Audience =
   `https://<appliance>/mcp`*, then add it to the realm's **default optional**
   client scopes. In Entra ID this is an *Application ID URI* on an app
   registration; in Okta a custom authorization server whose audience is that
   URL.
2. **`sub` and `preferred_username` in the access token.** Keycloak 25 moved
   `sub` into the built-in `basic` client scope; a realm imported without it
   produces tokens the appliance refuses for having no subject.
   `preferred_username` must be the person's work email, because that is what
   the mirrored source ACLs name; the appliance matches a caller against
   mirrored group members on both the subject and the username claim
   (`security.subject_claim` / `security.username_claim`).
3. **A way for clients to register.** MCP clients self-register at the IdP's
   `registration_endpoint`. The shipped realm permits this from loopback
   addresses and `claude.ai`/`claude.com` only (Keycloak *Client registration
   → Anonymous access policies → Trusted Hosts*), so a client that would
   redirect an authorization code to an attacker's domain is refused. Firms
   that prefer no anonymous registration can pre-register one public client
   (the shipped realm's `knowledge-index-mcp` is that client) and have people
   enter its client id.

### Verifying it end to end

```bash
# 1. The challenge that starts a login.
curl -s -D- -o /dev/null -X POST localhost:8000/mcp
#    HTTP/1.1 401 Unauthorized
#    www-authenticate: Bearer resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"

# 2. The metadata a client reads next.
curl -s localhost:8000/.well-known/oauth-protected-resource/mcp

# 3. A real token, then a real tool call. (Password grant stands in for the
#    browser handshake; it is enabled on the development realm's MCP client only.)
TOKEN=$(curl -s -X POST \
  http://localhost:8083/realms/knowledge-index/protocol/openid-connect/token \
  -d client_id=knowledge-index-mcp -d grant_type=password \
  -d username=lit.user@example.com -d password=lm-dev-only \
  -d 'scope=openid knowledge-index-mcp' | jq -r .access_token)

curl -s -X POST localhost:8000/mcp/ \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"search_filter","arguments":{"limit":100}}}'
```

The development realm ships `lit.user@example.com` and `corp.user@example.com`
(password `lm-dev-only`) as two users with different group memberships, so
their tokens return different document sets and an unknown identity returns
none.

## Users & provisioning

The appliance deliberately keeps **no user database of its own**. Every caller
is authenticated by the identity provider, which supplies a stable user id and
`groups`. The app turns those into principals (`user:<sub>`, `group:<name>`,
and `role:admin` for members of the configured admin groups) and grants are
made against those exact strings on the [Access control](/product/access-control/)
page.

**Add a user.** Create the person once in the identity provider: Keycloak
console (`http://localhost:8083/admin`, realm `knowledge-index`) → *Users →
Add user*, set a password and group membership. Put admins in the
`knowledge-index-admins` group. On first sign-in their principals appear in
Access control automatically.

**Mirror users from other systems.** Point Keycloak at the existing directory
instead of maintaining users by hand:

- **LDAP / Active Directory:** Keycloak → *User Federation → Add LDAP
  provider*, set connection URL, bind DN, users DN, enable group sync, and
  schedule periodic sync so new hires and leavers propagate.
- **SCIM:** provision from an upstream IdP (Okta/Entra) into Keycloak via a
  SCIM connector; the same `groups` claim reaches the app unchanged.
- Because the app authorizes on `group:` principals, granting a project to a
  federated group covers every current and future member, with no per-user churn.

**Why grants use exact principals.** Authorization is a string match: a
mistyped or wrong-cased principal silently grants nothing (fails closed).
Prefer principals that already appear on mirrored source ACLs; the Access
control page marks them.

## Connector credentials

Connectors hold OAuth refresh tokens for the firm's document estate, so they
are stored encrypted (AES-256-GCM) rather than in plain source config.

`KI_CONNECTOR_CREDENTIAL_KEY` is **required**: a base64 32-byte key, supplied
to the app, worker and watcher. There is deliberately no fallback: a
deployment that quietly stored refresh tokens in the clear would be worse than
one that refuses to start.

```bash
openssl rand -base64 32          # generate; store in the firm's secret manager
```

To rotate it, run with the **old** key still in the environment, then swap:

```bash
ki rotate-connector-key --new-key "$NEW_KEY" --dry-run   # reports what would change
ki rotate-connector-key --new-key "$NEW_KEY"
# then set KI_CONNECTOR_CREDENTIAL_KEY=$NEW_KEY everywhere and restart
```

Rows are re-encrypted in one transaction. A row that cannot be decrypted is
reported and skipped rather than silently dropped; re-authorize that
connection from the admin UI.

## Syncing in operation

Sync never happens inside the request that asks for it. The sync button,
`ki sync`, the scheduler and the folder watcher all enqueue the same
orchestrated run, one per source:

```bash
curl -X POST localhost:8000/api/actions/sync \
  -H "x-ki-principals: user:local-admin,role:admin"
# -> 202 {"runs":[{"run_id":"…","source_id":"…","display_name":"Matters"}],"skipped":[]}
curl "localhost:8000/api/runs?limit=20" -H "x-ki-principals: user:local-admin,role:admin"
```

Add `-d '{"source_id":"…"}'` to sync one connection instead of all.

What to expect while it runs:

- The run is a `source-sync` row in `/api/runs` and on the pipeline page.
  `current_step` shows the live observation count. `progress` stays at 0 until
  the run finishes: a scan does not know how many objects an estate holds
  until it reaches the end, and the appliance does not invent a bar.
- A **second sync while one is in flight is refused**, not queued; the source
  comes back under `skipped` with the id of the run already working on it. The
  rule is a database constraint, so it holds across the app, the CLI and the
  watcher.
- A failed scan leaves the run `failed` with the cause and sets the source to
  `error`. Other sources are unaffected. Fix the cause (almost always a
  revoked scope or an expired credential); the source retries on its own
  interval, so a fixed credential heals the connection by itself.

**Continuous sources sync themselves.** A connection with
`{"mode": "continuous", "interval": "2m"}` is enqueued every two minutes by
the scheduler, whatever kind of connector it is. `{"mode": "manual"}` means
"only when I ask". `KI_SYNC_SCHEDULE_SECONDS=0` turns scheduling off
completely.

**Provider events are the doorbell; the delta feed is the ledger.** A
notification enqueues the same `source-sync` run with `trigger=event`; the
event body is never trusted as indexed state. Setup:
[Microsoft 365](/connectors/microsoft-live-events/) ·
[Google Drive](/connectors/google-drive-live-events/).

**Full scans establish the delta checkpoint**, and a periodic full crawl still
happens every `security.acl_refresh_hours` so permission-only revocations
cannot persist. **Large deletions are confirmed across syncs** before anything
is tombstoned. **Handoff to processing** is automatic
(`pipeline.auto_insert_after_sync`, on by default); a firm that wants a
partner to review the scanned estate before paying for conversion and
embedding turns it off.

## Connector permissions

Every connector except Notion mirrors source ACLs. A source that cannot read
permissions yields *unknown*, which is fail-closed: its documents are not
retrievable until permissions can be read or an administrator adds a local
grant.

Two settings decide how mirrored permissions combine with local grants:

- `security.source_acl_mode`: `sufficient` (default) honours the source's
  word on its own; `intersect` additionally requires a local project or
  document grant. Prefer `intersect` where ethical walls are load-bearing.
- `security.acl_refresh_hours` (default 24) forces the full scan that re-reads
  ACLs.

Where a source cannot report group memberships, `security.principal_aliases`
maps a source group onto a local one:

```json
{"security": {"principal_aliases": {"group:entra:2b1f…": "group:litigation"}}}
```

## Production checklist

- TLS + real secrets everywhere (`LITELLM_MASTER_KEY`, `KI_POSTGRES_PASSWORD`,
  Keycloak admin, cookie secrets). No dev defaults in production; the compose
  file's defaults are development-only.
- OIDC auth mode (`KI_AUTH_MODE=oidc`) with the firm's identity provider;
  never expose port 8000 directly.
- `KI_CONNECTOR_CREDENTIAL_KEY` and the backup encryption key in the firm's
  secret manager; backup destination on other hardware
  ([Backup & restore](/operations/backups/)).
- Pin images by digest, generate an SBOM, rerun the license gate.
- Load-test retrieval at the firm's corpus size before quality or latency
  commitments; the invariant that matters most is zero cross-wall hits under
  concurrent load.
