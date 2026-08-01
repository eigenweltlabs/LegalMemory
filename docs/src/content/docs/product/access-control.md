---
title: Access control
description: Technical reference for the LegalMemory permission model, the access check endpoint, local grants and deny walls, source group aliases, mirrored group membership, and the security settings that govern them.
---

Access control in LegalMemory is compiled before retrieval: the permission
evaluator produces the SQL and search-index filter first, and lexical or vector
scoring runs only inside that filter. Retrieval never fetches a global result
set and removes unauthorized hits afterwards. This page documents the model as
implemented in `permissions.py`, the connector ACL mirror, and the Access
control page of the console. Identity itself (how a caller gets a validated set
of principals) is covered in [Sign-in](/product/sign-in/); deployment of the
identity gateway in [Deployment](/operations/deployment/).

## Principals

A principal is a plain string of the form `<kind>:<value>`. All principals are
stripped of surrounding whitespace and casefolded before storage and before
comparison; matching is otherwise exact string equality. There are no
wildcards and no prefix matching.

| Form | Example | Produced by |
|---|---|---|
| `user:<subject or email>` | `user:ursula@firm.example` | Sign-in token (`sub` claim, and the verified `email` claim); mirrored source ACLs (sources name viewers by email); local grants |
| `username:<name or email>` | `username:ursula@firm.example` | Sign-in token (`preferred_username` claim; verified email is carried in both spellings) |
| `user:id:<opaque id>` | `user:id:8f3a…` | Mirrored source ACLs when a source reports only a directory id, never an address |
| `group:<id>` | `group:litigation` | Sign-in token (`groups` claim, leading/trailing `/` stripped); local grants |
| `group:entra:<guid or email>`, `group:google:<email>` | `group:entra:2b1f…` | Mirrored source ACLs. The `entra` and `google` namespaces are treated as globally unique and stored as-is |
| `group:src.<source-id>.<name>` | `group:src.42.sp:owners` | Mirrored source ACLs for every other group namespace. A source-local name such as a SharePoint site's "Owners" is bound to its source id before storage, so identically named groups at two sources can never match each other |
| `role:authenticated` | — | Added to every successfully authenticated identity. Also written as a mirrored allow when a source marks an object organization-wide (`is_public`), which means "every authenticated caller of this appliance", never anonymous access |
| `role:admin` | — | Minted at identity resolution when the caller's groups intersect `security.admin_groups`. Never stored as a claim |
| `service:<name>` | — | Registered external API clients |

The evaluator's administrator set is `{"role:admin", "system:admin"}`
(`ADMIN_PRINCIPALS`); holding either casefolded string makes `is_admin` true.

## Where grants live

Three tables hold allow/deny decisions; a fourth mirrors group membership.

| Table | Scope | Written by | Row fields |
|---|---|---|---|
| `project_grants` | Project | The console (grant/wall forms, project creation) via `POST /api/projects/{id}/grants` | `principal`, `principal_kind`, `effect` (`allow`/`deny`), `role` (`viewer`/`editor`/`admin`/`owner`), `origin` (`manual`), `external_id` |
| `document_grants` | Single document | The console via `POST /api/documents/{id}/grants` | Same fields as project grants |
| `source_object_grants` | One observed source object | Connector sync (`origin: connector`); atomically replaced per object on every observation | `principal`, `principal_kind`, `effect`, `origin`, `external_id` |
| `source_group_members` | One source's directory | Connector sync after a full scan; replaced wholesale per source | `group_id`, `member_id`, `member_type` (`user`/`group`), `group_name`, `synced_at` |

Grant principals are casefolded on write (`payload.principal.casefold()`).
Connector-origin group principals in `source_object_grants` are source-scoped
(`qualify_principal`) before storage; operator-written grants are stored as
typed, since they already name principals in the appliance's own namespace.

Writing a project grant requires a managing role on that project
(`can_manage_project`: appliance administrator, or an `allow` project grant
with role `owner` or `admin`). Writing a document grant requires managing the
owning project — or appliance administrator rights, which also covers
documents that no project owns yet. The API exposes no endpoint that removes a
grant. After each grant write, the server refreshes the per-chunk ACL
projection used by the search index (`allowed_principals`,
`denied_principals`, `access_version` on every chunk of the affected
document(s)).

## Principal expansion

Before evaluation, the caller's principal set is expanded
(`AccessService.resolve_principals`), in this order:

1. Normalize: strip and casefold every principal.
2. Apply the alias map (`security.principal_aliases`). Aliases are additive —
   they add principals, never remove any. `user:` and `username:` are treated
   as interchangeable spellings on both sides of the map, and the map is also
   applied in reverse (a caller holding the alias target also gains the source
   key).
3. Expand mirrored group memberships (`expand_with_memberships`): the values
   of the caller's `user:`, `username:` and `group:` principals are looked up
   against `source_group_members.member_id`; each match adds
   `group:<group_id>`. Group-in-group edges are walked transitively.
4. Apply the alias map again (aliasing before the membership lookup is what
   lets an alias bridge a source-side address; aliasing again afterwards keeps
   group-to-group aliases working). Both passes are additive and idempotent.

## Evaluation

`AccessService.version_predicate` is the single evaluator. Every listing,
retrieval and search path uses it (directly or through `compile_scope`, which
turns it into an explicit document-id filter for the search index — an empty
scope compiles to `match_none`).

Given the expanded principal set:

1. **Administrator** (`role:admin` or `system:admin` held): the predicate
   reduces to "the version has at least one active (non-tombstoned) source
   observation". Administrators bypass every grant check, but not lifecycle: a
   version whose observations are all tombstoned is not a live document for
   anyone.
2. **Empty set** (the caller's principals matched nothing after
   normalization): the predicate is constant-false. Nothing is visible.
3. **Otherwise** a version is visible if and only if all three of the
   following hold, where every match is `principal IN (expanded set)` and
   source grants only count on active observations:

| Condition | Definition |
|---|---|
| `any_allow` | A project allow **or** a document allow — or, in `sufficient` mode only, a mirrored source allow |
| `source_intersection` | A mirrored source allow, **or** the version is observed on a `local_fs` source object that has no grant rows at all (local sources deliberately delegate to the local project/document boundary) |
| `no_deny` | No project deny **and** no document deny **and** no mirrored source deny for any held principal |

Deny therefore wins at every scope: a single deny row matching any held
principal — local or mirrored — defeats every allow. A deny naming a principal
the caller does not actually hold has no effect on that caller.

The `source_intersection` condition also means a local allow can never punch
through a known external ACL: for a non-`local_fs` source object that carries
grant rows, the mirrored ACL must allow the caller in *both* modes.

Project visibility (`visible_project_ids`) follows: administrators see all
projects; everyone else sees projects with a direct project allow not
cancelled by a project deny, plus any project containing a document they can
reach.

## `source_acl_mode`: `sufficient` vs `intersect`

The mode changes exactly one term of the predicate — what counts as
`any_allow`:

| Mode | `any_allow` | Consequence |
|---|---|---|
| `sufficient` (default) | project allow ∨ document allow ∨ source allow | A mirrored source allow is enough on its own. Faithful to the source — but an over-broad share there (a document shared with the whole organization) makes the document readable by every user of the appliance, bypassing local matter restrictions |
| `intersect` | project allow ∨ document allow | A mirrored source allow **and** a local project/document allow are both required for externally hosted sources. An over-broad share at source cannot defeat an ethical wall, at the cost of every external source needing local grants before anything is retrievable |

`local_fs` sources without a readable object ACL are unaffected by the mode:
they delegate to the project/document boundary either way. The mode is
validated to `sufficient|intersect`; any other value raises at configuration
time.

## Unknown ACLs

"Unknown" is defined at the connector boundary. A connector that could not
read an object's permissions at all returns `None` — deliberately distinct
from an empty viewer list, which asserts "nobody may see this". Per-source
translation drops any single permission entry it cannot resolve to a
principal, and `None` is reported as a capability gap rather than converted
into an empty list.

At the data level, `None` results in a source object with **zero rows** in
`source_object_grants`. The evaluator then fails closed structurally: for any
non-`local_fs` source object without grant rows, `source_intersection` cannot
be satisfied (there is no source allow, and the local-source delegation
applies only to `local_fs`), so the document is invisible to every
non-administrator regardless of local grants and regardless of mode. Documents
with unknown ACLs vanish; they are never over-shared.

`security.unknown_acl_policy` (`deny`/`allow`, default `deny`) is stored in
the configuration and editable in the console's identity gateway panel. The
current evaluator does not branch on it: the compiled predicate enforces the
`deny` behavior unconditionally, as described above.

## The access check: `GET /api/access/explain`

The console's "Can this person see it?" panel is backed by
`GET /api/access/explain` (administrator only).

| Parameter | Meaning |
|---|---|
| `principal` (required) | The principal to evaluate, e.g. `user:ursula@firm.example` |
| `query` (optional) | Case-insensitive substring filter on document titles |
| `limit` (optional, default 60, clamped 1–200) | Number of documents listed, most recently updated first |

The endpoint is read-only and uses the same `resolve_principals` and
`version_predicate` as the retrieval paths — the verdict cannot drift from the
real decision. Grant rows are returned as evidence, never re-evaluated.

Top-level response fields:

| Field | Content |
|---|---|
| `principal` | The principal as asked |
| `resolved` | The full expanded principal set (aliases and memberships applied) |
| `is_admin` | Whether the expanded set contains an administrator principal |
| `source_acl_mode` | The active combination mode |
| `groups` | The mirrored groups the caller expands into: `principal`, `group_id`, `label` (the source's own group name, suppressed when it merely repeats the id), `source`, up to 8 `members` plus `member_count`, `direct` (direct member vs reached through a nested group), and `documents` (documents that group's allows open) |
| `local_grants` | Every project and document grant naming a held principal: `scope`, `target_id`, `target`, `principal`, `effect`, `role`, `origin` |
| `documents` | `visible` (count over the whole corpus), `total`, `listed`, and per-document `items` |

Per-document verdicts (`documents.items[]`):

| Field | Content |
|---|---|
| `visible` | Whether the compiled predicate admits the document |
| `allowed_by` | The allows that matched a held principal, each `{scope: source\|project\|document, principal, …}` |
| `denied_by` | The denies that matched a held principal |
| `source_allows` | Every mirrored allow on the document, with the mirrored member count of each group — the answer to "what membership would open this" |

The console renders three states: **visible** (green), **denied** (red,
`denied_by` non-empty — an explicit deny matched), and **blocked** (neither —
no allow matched; the row lists the source groups whose membership would open
the document, flagging groups with nobody mirrored). When `is_admin` is true
the panel states explicitly that the principal holds `role:admin`, which
skips project, document and source grants entirely, rather than presenting
the full-corpus result as ordinary access. A principal that is in no mirrored
group and named by no local grant is called out as reaching nothing.

## Grants and walls

The console offers two separate write forms, both administrator-only in the
UI and both writing through the grant endpoints above:

- **Exception** (`effect: allow`): gives access the source did not, on a
  project or a single document, with a role (`viewer`, `editor`, `admin`,
  `owner`; `owner`/`admin` also confer project management). Subject to the
  evaluator's rules: in either mode, a local allow on an externally sourced
  document only takes effect alongside a mirrored source allow.
- **Wall** (`effect: deny`): an explicit deny on a project or a single
  document. A deny beats every allow — mirrored source allows included — at
  evaluation time, for any caller holding the denied principal. The form
  requires explicit confirmation before writing. Because the deny is matched
  against the caller's *expanded* principal set, a wall written against a
  group also walls off everyone expanded into that group.

Both forms match by exact (casefolded) string: a wall against a principal the
person does not actually hold walls off nobody, and an exception for a
mistyped principal grants nothing. The principal picker
(`GET /api/principals`) exists for this reason: it enumerates principals seen
on mirrored source ACLs, existing grants, the mirrored directory (groups, and
up to 2,000 users), registered service clients, and the configured
administrator groups, marking source-observed principals as the safest grant
targets.

Projects are optional local boundaries: a project groups documents so one
grant covers all of them. Creating a project writes an `allow`/`owner` grant
for the creator. A project with no grants of its own restricts nothing;
members reach its documents through the mirrored source ACLs.

## Source group aliases

`security.principal_aliases` is a flat string-to-string map, e.g.
`{"group:entra:2b1f…": "group:litigation"}`. It exists for sources that cannot
enumerate group memberships and for pinning a source group to a group the
sign-in provider already asserts. It is applied inside `resolve_principals` —
before *and* after membership expansion — additively and in both directions,
with `user:`/`username:` treated as interchangeable spellings. An alias is a
namespace bridge, not an access decision: it can only add principals to the
caller's set; the grant rows still decide.

The console's "Source group aliases" panel shows the map entries where either
side is a `group:` principal, prefixes bare names with `group:`, and saves via
`PUT /api/config`. A principal may not alias itself.

## Administrators and the identity gateway

The "Who runs this appliance" panel edits `security.admin_groups` and, under
"Identity gateway", the authentication settings. Administrator group names are
stored bare (the console strips any `group:` prefix and surrounding slashes);
at identity resolution they are compared casefolded against the caller's
groups, and a match adds `role:admin`. With the list empty, nobody is promoted
to `role:admin` by group membership. `role:admin` grants the evaluator's full
bypass and every administrator-only endpoint on this page.

Runtime effect of saving these settings:

- **Applied on the next request**: `admin_groups`, `auth_mode`,
  `trusted_header_name`, `subject_claim`, `username_claim`, `groups_claim`,
  `oidc_issuer`, `oidc_audience`. The identity resolver is constructed from
  the current configuration on every request.
- **Installed into the evaluator at process start**: `source_acl_mode` and
  `principal_aliases` are process-wide evaluator defaults, installed once by
  `configure_access` when the API server or CLI starts — deliberately, so no
  endpoint can evaluate under a different permission model than the rest of
  the appliance. A save through `PUT /api/config` (which is what this console
  page uses) persists them for the next start. The
  `POST/DELETE /api/identity/aliases` endpoints additionally re-run
  `configure_access`, so alias changes made through them take effect
  immediately.
- **Read per sync run**: `acl_refresh_hours`.

## Mirrored group membership

Grants in a firm name groups, not individuals, so a mirrored
`group:entra:<guid>` allow is unenforceable until the appliance knows who is
in that group. After each successful full scan of a source whose connector
supports ACLs, the sync engine replaces that source's `source_group_members`
rows wholesale — replaced, not merged, so a member removed at source loses
the access, and an empty snapshot is honored as a real state. Group ids are
qualified on the way in exactly as ACL principals are (`entra`/`google`
namespaces kept global, everything else bound as `src.<source-id>.<name>`),
so the two sides always meet.

Matching at evaluation time: sources report members by email, while the
appliance authenticates by OIDC subject. The identity resolver therefore
carries both `user:<sub>` and `username:<preferred_username>` — plus both
spellings of the verified `email` claim — and `expand_with_memberships` looks
up the value part of every `user:` and `username:` principal against
`member_id`. Whichever spelling equals the mirrored address (in practice, the
email) produces the match. `member_type: group` rows are group-in-group edges
and are walked transitively.

`security.acl_refresh_hours` bounds staleness: a delta feed reports content
changes, and a permission change at source alters no document's etag, so only
a full scan re-reads ACLs and memberships and notices a revocation. When a
source with ACL support has gone longer than `acl_refresh_hours` since its
last full scan (tracked in `sources.last_full_sync_at`), the engine forces a
full scan instead of an incremental one. A value ≤ 0 disables the forcing.

## Settings

All keys live under `security` in the configuration. Environment variables
use the `KI_` prefix with `__` as the nesting delimiter and pin the setting
against console edits.

| Config key | Environment variable | Default | Effect |
|---|---|---|---|
| `security.source_acl_mode` | `KI_SECURITY__SOURCE_ACL_MODE` | `sufficient` | How mirrored source ACLs combine with local grants (`sufficient`/`intersect`); see above |
| `security.unknown_acl_policy` | `KI_SECURITY__UNKNOWN_ACL_POLICY` | `deny` | Validated `deny`/`allow` and shown in the gateway panel; the current evaluator enforces the deny behavior structurally and does not read this key |
| `security.principal_aliases` | `KI_SECURITY__PRINCIPAL_ALIASES` | `{}` | Additive principal alias map applied during expansion |
| `security.acl_refresh_hours` | `KI_SECURITY__ACL_REFRESH_HOURS` | `24` | Maximum age of mirrored ACLs before a full rescan is forced; ≤ 0 disables |
| `security.admin_groups` | `KI_SECURITY__ADMIN_GROUPS` | `["knowledge-index-admins"]` | Provider groups promoted to `role:admin` at sign-in (casefolded match) |
| `security.auth_mode` | `KI_SECURITY__AUTH_MODE` | `trusted_header` | `trusted_header` (identities asserted by a reverse proxy) or `oidc` (bearer-token validation) |
| `security.trusted_header_name` | `KI_SECURITY__TRUSTED_HEADER_NAME` | `x-ki-principals` | Header carrying the comma-separated principal list; absent, standard `x-auth-request-*`/`x-forwarded-*` proxy headers are used |
| `security.trusted_header_secret` | `KI_SECURITY__TRUSTED_HEADER_SECRET` | unset | When set, requests must also carry a matching `x-ki-proxy-secret` |
| `security.subject_claim` | `KI_SECURITY__SUBJECT_CLAIM` | `sub` | Token claim becoming `user:<value>` |
| `security.username_claim` | `KI_SECURITY__USERNAME_CLAIM` | `preferred_username` | Token claim becoming `username:<value>` |
| `security.groups_claim` | `KI_SECURITY__GROUPS_CLAIM` | `groups` | Token claim becoming `group:<value>` principals |
| `security.oidc_issuer` | `KI_SECURITY__OIDC_ISSUER` | `http://keycloak:8080/realms/knowledge-index` | Required `iss` of accepted tokens; also the default base for the JWKS URL |
| `security.oidc_audience` | `KI_SECURITY__OIDC_AUDIENCE` | `knowledge-index` | Required `aud`; with no audience configured, tokens are refused outright |

## Endpoints

All endpoints require an authenticated identity; "admin" means the identity
must hold `role:admin`.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/access/explain?principal=&query=&limit=` | admin | Evaluate one principal against the corpus; returns the expanded set, group chains, matching grants and per-document verdicts |
| `GET /api/principals` | admin | Enumerate grantable principals with kind, grant counts, origins and source-observed flag |
| `GET /api/projects/{id}/grants` | project manager | List a project's grants |
| `POST /api/projects/{id}/grants` | project manager | Create a project allow or deny (`principal`, `principal_kind`, `effect`, `role`) |
| `POST /api/documents/{id}/grants` | admin, or manager of the owning project | Create a document allow or deny; admins may grant on documents no project owns |
| `POST /api/projects` | authenticated | Create a project; the creator receives an `allow`/`owner` grant |
| `GET /api/config` / `PUT /api/config` | admin | Read and save the configuration, including every `security.*` key on this page; saves refuse to overwrite environment-pinned settings (409) |
| `POST /api/identity/aliases` / `DELETE /api/identity/aliases` | admin | Add or remove a single principal alias; re-installs the evaluator's alias map immediately |

## Edge cases

- **Case sensitivity.** Everything is casefolded: caller principals, grant
  rows, alias keys and values, membership rows, and connector-mirrored
  viewers. Letter case can never distinguish two principals.
- **String exactness.** Beyond casefolding, matching is exact string
  equality. A mistyped principal in a grant or wall silently matches nobody;
  from outside, that is indistinguishable from a deliberate denial, which is
  why the access check exists.
- **A principal that matches nothing.** If a caller's expanded set names no
  grant row anywhere, every non-admin path returns empty: the predicate for
  an empty normalized set is constant-false, and a non-empty set with no
  matching allows fails `any_allow`. The access check reports 0 reachable
  documents and the console labels the principal as reaching nothing, with
  the per-document rows naming the memberships that would change that.
- **Deny scope.** A deny only affects callers whose expanded set contains the
  denied string. It is absolute for them and irrelevant for everyone else.
- **Local allows vs external ACLs.** A project or document allow never
  overrides a known external ACL: non-`local_fs` observations always require
  a mirrored source allow (`source_intersection`), in both combination modes.
- **Tombstones.** Source observations marked deleted are excluded from every
  ACL join, and a version with only tombstoned observations is invisible even
  to administrators — it is retained for restoration and audit, not served.
- **Group name collisions.** Because non-global group namespaces are bound to
  their source id, two sources' identically named groups (`sp:owners` at two
  sites) can never grant across each other.
- **Duplicate grants.** Grant rows are unique per target, principal, effect
  and origin; the connector's ACL snapshot replacement deduplicates on
  `(principal, effect)` and skips no-op updates entirely.
- **Organization-wide shares.** A source object shared tenant-wide is
  mirrored as an allow for `role:authenticated`, which every signed-in caller
  holds. Anonymous sharing links are deliberately not mirrored as grants.
