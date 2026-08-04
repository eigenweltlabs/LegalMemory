---
title: Sign-in
description: "How the console drives the bundled Keycloak realm over its admin REST API: identity providers, the token-claims checklist, the People table, aliases, and local password accounts."
---

**Sign-in** is the console page from which an administrator configures how the
firm's people authenticate. LegalMemory ships with a Keycloak realm and
drives it over Keycloak's admin REST API on the administrator's behalf: every
action on this page reads or writes realm objects directly, so nobody opens
Keycloak's own console. The realm administrator password and every identity
provider client secret stay in the app process; the browser only ever sees
what it typed.

The page is visible to administrators only (`role:admin`). All endpoints
below require it.

## What each console action writes to the realm

The appliance authenticates against the `master` realm (`admin_realm`) with a
password grant on the `admin-cli` client (`admin_client_id`), using
credentials read from the environment at call time
(`KI_KEYCLOAK_ADMIN_USERNAME` / `KI_KEYCLOAK_ADMIN_PASSWORD`). It then calls
`/admin/realms/{realm}/…` on the internal address (`identity.admin_base_url`).
Every write is idempotent: re-running setup (a second administrator, a
re-pasted rotated secret, a fresh stack against an existing realm) converges
on the same realm instead of failing or duplicating.

| Console action | Realm objects created or updated |
| --- | --- |
| Configure a provider | An identity-provider instance (`providerId: oidc`); two broker mappers on it; token-claim fixes on the clients listed below |
| Remove a provider | Deletes the identity-provider instance |
| Add person | A realm user (`username` = email, `emailVerified`, required action `UPDATE_PASSWORD`); a temporary password credential; a realm `passwordPolicy` if the realm had none |
| Reset password | A new temporary password credential; re-asserts `UPDATE_PASSWORD` |
| Enable / disable | The user's `enabled` flag |
| Delete person | Deletes the realm user |
| Link (alias) | Nothing in the realm; writes `security.principal_aliases` in the appliance's own config |

The identity provider client secret is written to Keycloak and additionally
stored in the appliance database as AES-256-GCM ciphertext (under the same key
as connector credentials, `KI_CONNECTOR_CREDENTIAL_KEY`). This is required
because Keycloak's admin API masks the secret on read, and a later
**Test sign-in** must re-present it to the provider. No endpoint returns it.

Every configure, test, remove, person and alias action is recorded as an
audit event (`identity.provider.*`, `identity.person.*`, `identity.alias.*`).
The temporary password is never included in an audit event.

## Identity providers

Four provider types are offered. All four are written into the realm as the
same generic Keycloak `oidc` broker, with the endpoints filled from the
provider's own discovery document rather than from Keycloak's branded social
providers, so every configured value is traceable to something the
administrator can check.

| Kind | Label | Extra field | Discovery URL |
| --- | --- | --- | --- |
| `google` | Google | none | `https://accounts.google.com/.well-known/openid-configuration` (fixed) |
| `entra` | Microsoft (Entra) | Directory (tenant) ID | `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration` |
| `okta` | Okta | Okta domain (host only, no `https://`) | `https://{domain}/.well-known/openid-configuration` |
| `oidc` | Other OIDC | Discovery URL (must start with `http://` or `https://`) | as pasted |

Every provider additionally asks for a **client ID** and a **client secret**.
The secret input is write-only; replacing credentials means pasting the secret
again. For `entra` and `okta`, a pasted full URL is stripped back to the host
automatically.

### What a save does, in order

`POST /api/identity/providers` performs these steps and stores nothing if any
of them fails:

1. **Fetch and validate the discovery document.** It must return HTTP 200,
   parse as JSON, and contain `issuer`, `authorization_endpoint`,
   `token_endpoint` and `jwks_uri`. This is the first proof that the tenant ID
   or Okta domain names a directory that exists. Failure → HTTP 400.
2. **Probe the client credentials** against the provider's token endpoint (see
   below). Rejection → HTTP 400; the broker is not written, because a broker
   that cannot log anybody in would still appear on the login page.
3. **Create or update the broker** (`POST`/`PUT`
   `/identity-provider/instances`). The instance uses
   `clientAuthMethod: client_secret_post`, `trustEmail: true` (the provider
   verified the address; a second confirmation mail would strand every user),
   `syncMode: FORCE`, default scopes `openid profile email`, and the issuer,
   authorization, token, JWKS, userinfo and logout endpoints copied from the
   discovery document.
4. **Ensure two broker mappers** on the instance:
   - `username-from-email` (`oidc-username-idp-mapper`, template
     `${CLAIM.email}`), which forces the imported Keycloak username to be the email
     address the provider asserts. This is the join key: connectors normalise
     every person to `user:<email>`, and access is decided by matching that
     string. A broker that imports someone as `ursula` while a source reports
     `ursula@firm.de` produces no error and no documents.
   - `email` (`oidc-user-attribute-idp-mapper`) copies the `email` claim to
     the user's email attribute.
5. **Ensure the token claims** the appliance requires (next section).
6. **Store the credential row** (client ID, encrypted secret, discovery URL,
   extra value, issuer) so a later test can re-verify against the provider.

### The redirect URI

Each provider card shows the **redirect URI to register**, including for
providers nobody has configured yet; it is the value a firm must register at
Google, Entra or Okta *before* it has a client ID and secret to paste back.
It is derived exactly as:

```
{identity.public_base_url}/realms/{identity.realm}/broker/{alias}/endpoint
```

where `alias` is the provider kind (`google`, `entra`, `okta`, `oidc`). With
defaults, the Google URI is
`http://localhost:8083/realms/knowledge-index/broker/google/endpoint`. It must
be registered at the provider character for character; a mismatch is the
single most common reason a first login fails.

### Test sign-in

`POST /api/identity/providers/{alias}/test` re-establishes each fact rather
than inferring it from what was saved: the discovery document is re-fetched,
the stored secret is re-presented to the provider, the broker is re-read out
of the realm, and a login is started the way a browser starts one. No browser
session is created by any probe.

| Check id | What is actually done | A failure means |
| --- | --- | --- |
| `discovery` | GET the stored discovery URL; validate JSON and the four required endpoints | The provider (or the pasted tenant/domain/URL) is unreachable, or the URL does not serve an OIDC discovery document |
| `credentials` | POST an `authorization_code` grant with a code that cannot exist to the token endpoint, carrying the stored client ID and decrypted secret. Per RFC 6749 §5.2, `invalid_client` / `unauthorized_client` / HTTP 401 means the credentials were rejected; `invalid_grant` or `invalid_request` means the provider authenticated the client and only rejected the fake code, which is the pass | The client ID or secret is wrong (revoked, rotated, mistyped) |
| `realm` | Read the broker instance back out of the realm; require it present, enabled, and its configured issuer equal to the freshly fetched one | The broker was deleted or disabled in Keycloak directly, or the realm points at a different issuer than the provider now reports |
| `client:<id>` (one per `token_client_ids`) and `audience` | The token-claims checklist, re-read (next section) | See next section |
| `login` | Start the authorization request at the realm's own `…/protocol/openid-connect/auth` with `kc_idp_hint=<alias>`, then follow the redirect chain. Keycloak's hops to its own canonical public URL are rewritten back to `admin_base_url` (the public name is unreachable from inside the compose network); the provider's hops are followed untouched. Success = the chain lands on the provider's authorization-endpoint origin and not on an error page | Keycloak stopped at its own broker page, the client is missing from the realm, or the provider refused; the reason is extracted from `error_description` / `error` in the redirect (Google's opaque `authError` blob is decoded for its readable fragments, e.g. `redirect_uri_mismatch`). If the provider gave no reason, the usual cause is an unregistered redirect URI |

The result is stored on the credential row (`last_tested_at`,
`last_test_ok`, per-check detail) and shown on the card as
tested / failing / untested. Provider response bodies are truncated to 300
characters before display, because they can contain tokens or assertions.

## The token-claims checklist

The "Token settings this appliance requires" panel asserts, on every save and
on every page load, the realm plumbing that a working OIDC login turns out to
depend on. Each item fails silently in Keycloak itself, which is why the
console keeps them visible:

| Check | Asserted on | Why it is load-bearing |
| --- | --- | --- |
| Full (non-lightweight) access tokens + a `sub` mapper | Each client in `identity.token_client_ids` (default `knowledge-index-ui`, `knowledge-index-mcp`) | The identity resolver reads the subject from `security.subject_claim` (default `sub`) and refuses a token without one. A lightweight access token carries no `sub`; Keycloak 25 moved `sub` out of the token core into the `basic` client scope, so a realm without that scope never had one |
| An `aud` mapper stamping `security.oidc_audience` | The client named by `identity.audience_client_id` (default `knowledge-index-ui`) | Token validation requires the expected audience; a token with no `aud` fails audience validation and the appliance rejects it. If a default client scope already carries the mapper, nothing is added |
| Client present in the realm | All of the above | A missing client is reported as "absent from the realm" and cannot be fixed from here |

These checks cover what the appliance's own writes control. The other two
claims the resolver consumes come from the shipped realm's client scopes and
are matched against mirrored ACLs:

- **`preferred_username`** (`security.username_claim`): produced by the
  `profile` scope; becomes the `username:<value>` principal.
- **`groups`** (`security.groups_claim`): produced by the `groups` client
  scope (group-membership mapper, `full.path: false`); each entry becomes a
  `group:<name>` principal, matched against mirrored group ACLs, and
  membership in any of `security.admin_groups` mints `role:admin`.

The subject becomes `user:<sub>`; when the token carries a verified `email`
claim, `user:<email>` and `username:<email>` are added as well, because
mirrored source ACLs name people by email while `sub` is an opaque realm ID.
A red check therefore means concretely: no `sub` → every login is refused
with "OIDC token has no subject"; no `aud` → every token is rejected at
validation; no `groups` → no group-based ACL matches and no administrator.

## The People table

`GET /api/identity/people` joins two lists that nothing else puts side by
side:

- **Realm users**, read over the admin API (default cap 200), with their
  federated-identity links (which broker each person signs in through; an
  empty list means a local password account).
- **Identities the connectors mirrored**: every user principal from
  `source_group_members` (directory membership) plus `source_object_grants`
  (per-object shares), casefolded and keyed by address. Only sources that
  report identities at all count as witnesses; a local folder mirrors no
  directory and is never counted against anybody.

Each row shows: username, last seen (recovered from the most recent audit
events naming that principal), sign-in route (broker aliases, or "password"),
and **source match**, matched in *n* of *m* witnessing sources. Aliases
already configured in `security.principal_aliases` are applied before
matching, so a bridged person reads as matched. Rows are sorted with the
fewest matches first.

**"Will see nothing" detection**: when at least one source reports identities
and a person matches none of them, the row is flagged and names the sources
that do not know the address. This is the failure the table exists to
surface: such an account works perfectly, raises no error anywhere, and
opens onto an empty index. The **Add person** form runs the same check while
the administrator is still typing the address, and the create response
repeats it.

| Action | Endpoint | Behaviour |
| --- | --- | --- |
| List people | `GET /api/identity/people` | The join described above; also returns the mirrored-identity index and the alias map |
| Add person | `POST /api/identity/people` | Validates the email (must be a real address, since it is the join key), sets a minimum realm password policy if the realm has none (`length(12) and notUsername`), creates the user with `username` = email and `emailVerified`, generates a 20-character temporary password (four blocks of five, look-alike characters removed), sets it as temporary with `UPDATE_PASSWORD` required. 409 if the address can already sign in |
| Reset password | `POST /api/identity/people/{id}/password` | Issues a new temporary password; the old one stops working immediately; the first-sign-in change is re-armed. Offered in the console for password accounts only |
| Enable / disable | `POST /api/identity/people/{id}/enabled` | Disable stops the account opening but keeps it and its history; refused (400) for the caller's own account |
| Delete | `DELETE /api/identity/people/{id}` | Removes the realm user permanently; refused for the caller's own account. Disabling is the reversible option |
| Link alias | `POST /api/identity/aliases` | Body `{principal, alias}` as `user:<address>` pairs; bridges a sign-in identity onto the identity a source reported for the same person. Additive only: an alias adds principals, never denies, so the worst a wrong entry does is fail to match. Stored in `security.principal_aliases`, not in the realm |
| Remove alias | `DELETE /api/identity/aliases?principal=…` | Removes the bridge |

Temporary passwords are shown exactly once, in the browser, and are never
stored, logged, or retrievable; a reset issues a new one. Local password
accounts exist for firms with no directory; when a broker is configured the
form says so and recommends it instead.

The self-guard (`is_self`) matches the realm account's id, username and email
against every name the caller holds (the OIDC subject, the username, and the
`user:` / `username:` principals), because behind an OIDC login the subject
is the Keycloak user ID while behind a trusted proxy it is the asserted
address. It is enforced by the endpoints, not just hidden in the UI.

## The auth chain in operation

Two ways into the appliance, selected by `security.auth_mode`:

- **Through oauth2-proxy** (compose port 8090, the production-style entry).
  oauth2-proxy performs the OIDC login against the realm's
  `knowledge-index-ui` client and forwards the result upstream as headers. In
  the default `trusted_header` mode the resolver, finding no
  `x-ki-principals` header, reads oauth2-proxy's headers instead, preferring
  the verified email (`x-auth-request-email`, `x-forwarded-email`) over the
  opaque OIDC user ID, then `preferred_username`, then the user header, and
  turns `x-auth-request-groups` into `group:` principals (Keycloak's leading
  `/` on group paths is stripped). If `security.trusted_header_secret` is
  set, the request must also carry a matching `x-ki-proxy-secret` header;
  this pins header trust to the proxy.
- **Direct to the app port** (compose port 8000). In `trusted_header` mode a
  caller may name its own principals in `x-ki-principals`
  (`security.trusted_header_name`), the development identity gate. The
  console participates: the UI attaches that header from
  `localStorage["ki.devPrincipals"]` or the `VITE_DEV_PRINCIPALS` build
  variable. Anyone who can reach the port becomes anyone, so this mode is for
  development or for deployments where only the proxy can reach the port. In
  `oidc` mode the API instead requires a bearer token, validated by
  signature (RS256/ES256) against `security.jwks_url`, with issuer
  `security.oidc_issuer` and audience `security.oidc_audience` both enforced
  (an empty audience is refused outright rather than accepting any client's
  token).

**Where `role:admin` comes from**: in both modes, the resolver adds
`role:authenticated` to every successful login and `role:admin` when the
caller's groups intersect `security.admin_groups` (default
`knowledge-index-admins`). Nothing else mints it. The Sign-in page, and every
admin endpoint, keys on it.

**MCP is different**: `/mcp` always requires a bearer token regardless of
`auth_mode`, because `trusted_header` is a statement about a proxy in front
of the admin UI and no proxy stands in front of an MCP client. The
development escape hatch `security.mcp_allow_trusted_header` (default
`false`) can relax this and must never be on for a firm's appliance. The full
OAuth resource-server flow (the 401 challenge, protected-resource metadata,
audience binding to the MCP resource) is documented in
[Deployment & identity](/operations/deployment/#signing-in-from-an-mcp-client).

## Configuration

Env vars follow the `KI_<SECTION>__<FIELD>` convention.

### `identity.*`

| Config key | Env var | Default | Effect |
| --- | --- | --- | --- |
| `identity.admin_base_url` | `KI_IDENTITY__ADMIN_BASE_URL` | `http://keycloak:8080` | Where **this appliance** reaches Keycloak, inside the container network. All admin API calls and the test login's probe go here |
| `identity.public_base_url` | `KI_IDENTITY__PUBLIC_BASE_URL` | `http://localhost:8083` | Where **browsers and identity providers** reach Keycloak. The broker redirect URI is derived from it, and the test login rewrites Keycloak's public-URL redirect hops back to the internal address |
| `identity.realm` | `KI_IDENTITY__REALM` | `knowledge-index` | The realm this page manages |
| `identity.admin_realm` | `KI_IDENTITY__ADMIN_REALM` | `master` | Realm the appliance's admin credentials authenticate against |
| `identity.admin_client_id` | `KI_IDENTITY__ADMIN_CLIENT_ID` | `admin-cli` | Client used for the admin password grant |
| `identity.audience_client_id` | `KI_IDENTITY__AUDIENCE_CLIENT_ID` | `knowledge-index-ui` | The client whose tokens the appliance validates; the audience mapper is asserted here, and the test login is started as this client |
| `identity.token_client_ids` | `KI_IDENTITY__TOKEN_CLIENT_IDS` | `["knowledge-index-ui", "knowledge-index-mcp"]` | Every client that mints a token for a person; each is asserted to issue full access tokens carrying `sub` |
| `identity.admin_username_env` | n/a | `KI_KEYCLOAK_ADMIN_USERNAME` | Name of the env var the admin username is read from, at call time |
| `identity.admin_password_env` | n/a | `KI_KEYCLOAK_ADMIN_PASSWORD` | Name of the env var the admin password is read from. Neither credential is ever written to `config.json` |

Both base URLs exist because they name the same server from two networks: the
appliance talks to Keycloak on the compose network, while the redirect URI a
firm registers at Google or Entra must carry the published name. `KC_HOSTNAME`
on the Keycloak container pins one canonical public URL so the broker
callback does not vary by caller.

### Relevant `security.*`

| Config key | Env var | Default | Effect |
| --- | --- | --- | --- |
| `security.auth_mode` | `KI_SECURITY__AUTH_MODE` | `trusted_header` | `trusted_header` or `oidc`, as described above |
| `security.oidc_issuer` | `KI_SECURITY__OIDC_ISSUER` | `http://keycloak:8080/realms/knowledge-index` | Must equal the token's `iss`. Tokens are minted for whoever asked, so this is usually the *public* issuer name |
| `security.oidc_jwks_url` | `KI_SECURITY__OIDC_JWKS_URL` | empty (derived from issuer) | Where signing keys are fetched. Separate from the issuer because the appliance fetches keys on the container network while `iss` carries the public name |
| `security.oidc_audience` | `KI_SECURITY__OIDC_AUDIENCE` | `knowledge-index` | Required in every API token's `aud`; the checklist asserts a mapper stamps it |
| `security.subject_claim` | `KI_SECURITY__SUBJECT_CLAIM` | `sub` | Claim that becomes the subject and the `user:<subject>` principal |
| `security.username_claim` | `KI_SECURITY__USERNAME_CLAIM` | `preferred_username` | Claim that becomes the username and the `username:<value>` principal |
| `security.groups_claim` | `KI_SECURITY__GROUPS_CLAIM` | `groups` | Claim whose entries become `group:<name>` principals |
| `security.admin_groups` | `KI_SECURITY__ADMIN_GROUPS` | `["knowledge-index-admins"]` | Membership in any of these mints `role:admin` |
| `security.trusted_header_name` | `KI_SECURITY__TRUSTED_HEADER_NAME` | `x-ki-principals` | Header a caller may use to assert principals in `trusted_header` mode |
| `security.trusted_header_secret` | `KI_SECURITY__TRUSTED_HEADER_SECRET` | unset | When set, requests must carry it in `x-ki-proxy-secret`; pins header trust to the proxy |
| `security.principal_aliases` | n/a (written by the console) | `{}` | The alias bridges managed by the Link action |
| `security.mcp_allow_trusted_header` | `KI_SECURITY__MCP_ALLOW_TRUSTED_HEADER` | `false` | Development-only: lets `x-ki-principals` authenticate MCP calls |

## What the shipped dev realm seeds

`deploy/keycloak/knowledge-index-realm.json` is imported on first start
(`start-dev --import-realm`) and contains, for development:

- **Groups**: `knowledge-index-admins`, `knowledge-index-users`, `ma-team`,
  `litigation`.
- **Users**: `admin@example.com` (in `knowledge-index-admins`) plus three
  sample users, all with the documented dev password `lm-dev-only`.
- **Clients**:
  - `knowledge-index-ui`: confidential client oauth2-proxy signs in with
    (dev secret `lm-dev-only`); redirect URIs for the `:8090/oauth2/callback`
    entry; lightweight access tokens disabled; default scopes include
    `groups` and `knowledge-index-api`.
  - `knowledge-index`: bearer-only client that exists as the API's audience.
  - `knowledge-index-mcp`: public client with PKCE (S256) for MCP clients
    that do not self-register; redirect URIs for local MCP tooling and
    `claude.ai` / `claude.com` callbacks.
- **Client scopes**: `basic` (carries `sub`, which Keycloak 25 moved here,
  and `auth_time`), `profile` (`preferred_username`, full name), `email`,
  `groups` (group-membership claim, names without the leading `/`),
  `knowledge-index-api` (stamps `aud: knowledge-index`), and
  **`knowledge-index-mcp`**, the scope that binds an access token to the MCP
  endpoint by stamping the resource URL (`http://localhost:8000/mcp` in dev)
  into `aud`, with a consent-screen entry. It is a realm-default *optional*
  scope, so any dynamically registered MCP client may request it.
- **Anonymous client-registration policy**: registrant URIs must match,
  trusted hosts limited to `localhost`, `127.0.0.1`, `claude.ai`,
  `claude.com`, `opencode.ai`; consent required; full scope disabled; at most
  200 clients.

How MCP clients use this (dynamic registration, the RFC 9728 metadata, the
audience check) is covered in
[Deployment & identity](/operations/deployment/#signing-in-from-an-mcp-client),
not here.

## Failure modes

| Symptom | Cause and behaviour |
| --- | --- |
| "Cannot reach the realm" banner; provider and people lists degrade | Keycloak unreachable at `identity.admin_base_url`. Reads report the error inline (`realm_error`); writes fail with 502/503. Error text names the address, never a secret |
| 503 naming `KI_KEYCLOAK_ADMIN_USERNAME` / `KI_KEYCLOAK_ADMIN_PASSWORD` | The admin credentials are not set in the deployment environment, so the appliance cannot configure the realm at all |
| "Keycloak refused the appliance's administrator credentials (401)" | Wrong admin username or password. The response body from Keycloak is deliberately not echoed, since a failed password grant can name the account |
| Save rejected with "…rejected the client id or secret" | The credentials probe failed at the provider's token endpoint; nothing was written to the realm or the database |
| Test sign-in: `login` check fails with `redirect_uri_mismatch` (or a provider error at its own error page) | The redirect URI registered at the provider does not match `{public_base_url}/realms/{realm}/broker/{alias}/endpoint` exactly. When the provider gives no reason in the redirect, an unregistered redirect URI is the stated usual cause |
| Test sign-in: "Keycloak stopped at its own broker page" | The realm-side broker chain is broken (alias, client, or imported endpoints), before the provider is ever reached |
| Token-claims check red | See [the checklist](#the-token-claims-checklist): missing `sub` refuses every login, missing `aud` rejects every token, and a client "absent from the realm" cannot be repaired from the console |
| Person signs in fine but sees nothing | No source reports their address. The People table flags the row; fix at the source, or use **Link** if the person exists there under a different address |
