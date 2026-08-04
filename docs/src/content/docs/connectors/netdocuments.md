---
title: NetDocuments
description: Index cabinets, workspaces and documents from NetDocuments, scoped by cabinet, with cabinet and workspace access mirrored.
---

The NetDocuments connector indexes **cabinets, workspaces and documents** from a
NetDocuments repository, mirroring the group access a firm has already set. A
cabinet the authorizing account cannot open is never listed, so it is never
indexed.

| | |
| --- | --- |
| Syncs | Cabinets, workspaces and folders (as containers), documents, document deletions |
| Incremental | Per-cabinet `modified` search, plus a cabinet-access diff |
| Scoping | Cabinets as roots |
| Permission mirror | Yes, with a documented gap — see [Permissions](#permissions) |
| Token type | Refresh token |
| Region | EU (`api.eu.netdocuments.com`) by default; per-connection override via the `api_base_url` field |

:::caution[Not yet offered in the admin UI]
This connector is built and tested against recorded payloads, but it has not yet
run against a live repository. Response *shapes* are inferred from the published
API definition, which documents paths and parameters but almost no response
bodies. It stays out of the connectable catalog until one live sync confirms
them. See [What is verified](#what-is-verified).
:::

## Get access to the API

Two separate things are needed, and neither is instant:

1. **A Developer Portal account**, which is what lets you register an
   application and read the full API reference. It is not self-service: open a
   support request at `support.netdocuments.com` with help-desk field **API
   Support** and subject **Request Dev Portal account**.
2. **A repository to read.** Firms have their own. For building and testing
   without one, the NetDocuments **ISV partner programme** is a public
   application form and its stated benefits include free use of your own
   NetDocuments repository.

## Register the application

In the [Developer Portal](https://netdocuments.force.com/login), Applications →
New:

1. **Application type** must be **REST**, and **client type**
   **Confidential** (Authorization Code grant). A public client cannot hold the
   client secret this connection needs.
2. **Redirect URI:** enter the appliance's callback exactly as shown in the
   setup modal, including scheme and port.
3. **Scopes:** requested automatically in the authorization URL; nothing to
   type. NetDocuments defines `read`, `organize`, `edit`, `delete_doc`,
   `delete_container`, `lookup` and `admin`. This appliance asks for **`read`
   alone** — `organize` would let it change ACLs and `edit` would let it alter
   the firm's documents. It needs neither and must not be able to do either.
4. **Credentials:** the application page shows the Client Id and Client Secret
   once saved.
5. **Service account mapping:** a repository administrator maps the
   application's client id to an account, in Repository Administration →
   Service Account. Until that mapping exists, sign-in succeeds and **every API
   call comes back empty** — which is the failure that looks like a broken
   connector and is not one.

## Region

NetDocuments runs isolated regional services and a repository lives in exactly
one of them. Region appears twice in this connection:

- the **API base URL** in the connection's settings, and
- the **token endpoint**, which is regional too.

EU firms use `https://api.eu.netdocuments.com`, US
`https://api.vault.netvoyage.com`, AU `https://api.au.netdocuments.com`. A
token issued for one region is rejected by another.

## Connect

Enter the client id and secret, then authorize with a **dedicated account whose
cabinet access is exactly what the firm wants searchable**. The connection can
never see more than that account. After authorization, pick the cabinets to
index in the scope picker.

## Permissions

NetDocuments secures content through **cabinet and workspace membership**: a
group holds view, edit, share or administer rights, or an explicit *no access*
row, which is how a firm builds an ethical wall.

The connector mirrors that membership onto cabinets and the containers inside
them, and expands each granted group into its members so a grant matches a real
caller. An explicit no-access row is dropped, never inverted into a grant.

**The gap worth understanding.** A NetDocuments document can carry an access
list of its own, narrower than the workspace it sits in — one restricted memo
inside an otherwise open matter. The connector reads that list when the
document profile carries it. Where it does not, the document stays
**fail-closed**: unknown access, retrievable by nobody until an administrator
grants it at the project level.

That default is deliberate. Falling back to the container's access would
publish exactly the overrides that exist in order to be narrower. A firm that
knows its repository does not use document-level overrides can opt in with
**Let documents inherit their container's access**, and should not otherwise.

## Incremental sync

NetDocuments has no delta feed, so an incremental run does two things:

- asks each synced cabinet for documents **modified since** the last watermark,
  which finds edits and additions; and
- re-reads each cabinet's **membership and diffs it** against the previous run.
  A wall is built by changing a cabinet, and no document timestamp moves when
  that happens — so without the diff, a re-permissioned cabinet would keep
  serving its old audience until the next full scan. A cabinet whose access
  changed re-emits its documents.

Deletions have no tombstone: a deleted document simply stops matching every
query. They are reconciled from the previous run's per-container ids. An
**empty or unreadable cabinet listing is never treated as deletion** — it asks
for a full sync instead, because a withdrawn grant and an outage both arrive
looking like "no cabinets".

## What is verified

| | |
| --- | --- |
| OAuth endpoints and scope list | Verified — from the published API definition; regional API hosts confirmed live |
| Paths, parameters, pagination | Verified against the published API definition |
| Response body shapes | **Inferred.** The definition documents almost no response bodies |
| Per-document access list | **Unconfirmed.** Read defensively; absent means fail-closed |
| Live repository sync | **Not yet run** |
