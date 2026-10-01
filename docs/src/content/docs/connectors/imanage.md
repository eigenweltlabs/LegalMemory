---
title: iManage Work
description: Index workspaces, folders and documents from iManage Work, scoped by matter, with workspace, folder and document security mirrored.
---

The iManage Work connector indexes **workspaces, folders and documents** from an
iManage estate, mirroring the security a firm has already set. A matter walled
away from the authorizing account is never listed, so it is never indexed.

| | |
| --- | --- |
| Syncs | Workspaces and folders (as containers), documents, document deletions |
| Incremental | Per-library `edit_date` search, plus a workspace-security diff |
| Scoping | Library, then workspace (the matter) |
| Permission mirror | Yes, including iManage's own inherit/override distinction |
| Token type | Refresh token |
| Host | `cloudimanage.com` by default; per-connection override via the `api_base_url` field |

:::caution[Not yet offered in the admin UI]
This connector is built and tested against recorded payloads, but it has not yet
run against a live tenant. The API operations and the security payload schema
come from a published definition; the native REST **paths** are partly inferred.
It stays out of the connectable catalog until one live sync confirms them. See
[What is verified](#what-is-verified).
:::

## Getting a tenant

There is no self-service route. Unlike most connectors here, you cannot sign up
and start building — registering an application requires an existing iManage
environment, and the API reference itself sits behind an iManage account.

In practice that means one of:

- **the firm's own tenant**, which is the normal case in production;
- **a firm's sandbox tenant**, which iManage Cloud customers run alongside
  production and can grant access to. This is the fastest route to a working
  build;
- **the iManage Technology Partner programme**, which is a sales-qualified
  contact form rather than a signup.

## Register the application

In **iManage Control Center → Applications**, signed in as a member of the
`NRTADMIN` group or with a role carrying Tier 2 Control Center privileges. No
other role can register an application.

1. **Add an application manually.** It is a third-party application that reaches
   iManage Work through the API only, so it needs no UI extension package.
2. **Redirect URI:** enter the appliance's callback exactly as shown in the
   setup modal.
3. **Keep it read-only.** This appliance mirrors security; it must never be able
   to change it.
4. **Credentials:** the application's configuration shows the **API Key** and
   **API Secret**. *Auto-Generate* creates the secret if you do not supply one —
   copy it then, it is not shown again.

## Connect

Enter the API key and secret, then authorize with a **dedicated account whose
workspace access is exactly what the firm wants searchable**. The connection can
never see more than that account.

The **customer id** every API path is scoped by is read from the authorizing
account at connect time; set it explicitly only where that lookup is not
available.

After authorization, pick libraries and then workspaces in the scope picker. Two
levels, because an estate has a handful of libraries and thousands of matters,
and a flat list of every matter is a picker nobody can use.

## Permissions

iManage states security as a `default_security` value plus an `acl` of trustees,
each carrying an access level.

- `read`, `read_write`, `full_access` and `change_security` confer read and
  become viewers. **`no_access` is the explicit denial an ethical wall is built
  from** and is never inverted into a grant. Anything unrecognised confers
  nothing.
- `public` means library-wide, which on this single-firm appliance is every
  authenticated user.
- Groups are expanded to their members, so a grant matches a real caller.
- A user trustee carries an iManage login id, not an address. Where the payload
  has an address it is used; otherwise the id lands in the `user:id:` namespace
  the identity layer reconciles, rather than being dropped or guessed at.

**What makes iManage easier than NetDocuments here:** iManage says on every
object whether it *inherits* its container's security. Inheritance is therefore
the source's own answer rather than a guess, so:

- an **inheriting** document takes its container's mirrored access, at no extra
  API call;
- an **overriding** document is read on its own, one call for that document only;
- an override that **cannot** be read stays fail-closed. It never falls back to
  the container, because an override generally exists in order to be narrower.

The same applies to folders: a restricted subfolder inside an open matter keeps
its own audience rather than being flattened to the workspace's.

## Incremental sync

No delta feed exists, so an incremental run does two things:

- searches each synced library for documents **edited since** the last
  watermark, which finds edits and additions; and
- re-reads each workspace's **security and diffs it** against the previous run.
  A firm walls a matter by re-securing the workspace, and no document's
  `edit_date` moves when that happens. A workspace whose security changed
  re-emits its documents.

Deletions have no tombstone and are reconciled from the previous run's
per-workspace ids. An **empty or unreadable workspace listing is never treated
as deletion** — it asks for a full sync instead.

## What is verified

| | |
| --- | --- |
| Operations, and the security payload schema | **Verified** — the published definition declares the ACL schema in full |
| Access levels and `default_security` values | **Verified** — enumerated in that schema |
| Document, folder and workspace profile fields | **Verified** — declared schemas |
| `folders` / `subfolders` path shape | **Verified** — matches published third-party integration docs |
| `/security`, `/documents/search`, `/groups/{id}/members` paths | **Inferred** — the published definition describes a facade, not the native REST paths |
| `X-Auth-Token` header, OAuth endpoints | **Inferred** |
| Live tenant sync | **Not run** |
