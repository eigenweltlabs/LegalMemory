---
title: Connecting a source
description: "What every connector shares: bring-your-own-client OAuth, encrypted credentials, scoping, permission mirroring, and sync behaviour."
---

Every connector reads a source **read-only** and mirrors its permissions into
the index. This page covers what all of them share; each connector's own page
has the provider-console steps.

## Available connectors

The connectors that can be connected from the console:
[SharePoint Online](/connectors/sharepoint-online/),
[OneDrive](/connectors/onedrive/), [Google Drive](/connectors/google-drive/),
[Dropbox](/connectors/dropbox/), [Clio](/connectors/clio/), and
[local folders](/connectors/local-folders/).

Everything else in the catalog is greyed out and cannot be connected. For
customer-specific DMS integrations there is a
[plugin contract](/development/plugin-connectors/).

## Bring-your-own-client OAuth

Cloud connectors authenticate with OAuth 2, and every deployment is
**bring-your-own-client**: the firm registers its own app in the provider's
console and supplies the client id and secret per connection. There is no
vendor-hosted OAuth broker and no cloud auth middleman. Nothing third-party
sits on the path to the firm's documents.

Three consequences:

1. **You register an app once per provider.** The setup modal in the admin UI
   shows the registration steps for the exact provider, including the scopes,
   generated from the same data the appliance uses at authorization time, so
   the instructions cannot drift.
2. **The redirect URI is derived, not typed.** It is
   `KI_PUBLIC_BASE_URL` + `/api/connectors/oauth/callback`; the UI displays
   the exact string to paste into the provider console. Set the public base
   URL before registering.
3. **Scopes are read-only minimums.** Each connector requests the smallest
   read-only grant that supports building the index and mirroring permissions.
   Nothing ever requests write access to a source.

## Credential encryption

`KI_CONNECTOR_CREDENTIAL_KEY` (base64, 32 bytes) is **required**: every stored
OAuth token and secret is encrypted with AES-256-GCM under it. There is
deliberately no fallback to plaintext storage. Rotation is supported
(`ki rotate-connector-key`); losing the key means re-authorizing every
connection.

## Scoping what is synced

Connectors that support scoping (SharePoint, OneDrive, Google Drive, Dropbox,
Clio) let
you pick subtree roots (sites, libraries, folders, or matters) in a tree picker
that browses the real source after authorization. Scoping is
proportionality: index the estate, not the birthday-party photos.

Re-scoping behaves predictably: narrowing tombstones the excluded documents
immediately; widening forces a full scan; a root that disappears at the source
is a warning, never a silent fallback.

## Permission mirroring

Every connectable connector mirrors source ACLs. Group memberships are
mirrored too, so a document shared with a team is visible to exactly that
team's members. A source that cannot report permissions yields *unknown*,
which **fails closed**: those documents are not retrievable until an
administrator adds a local grant.

Personal-corpus connectors (such as OneDrive) scope everything to the drive
owner; granting one of these to a whole group is refused without explicit
confirmation, because it would publish one person's corpus.

## Sync behaviour

- The first sync is a full crawl that stages content and permissions and
  stores a provider checkpoint (Graph delta link, Drive changes token, …).
- Later syncs drain the provider's change feed where one exists; others do
  full rescans on the connection's interval.
- A full ACL-refreshing scan runs at least every `security.acl_refresh_hours`
  (default 24), because a permission change alters no document's etag.
- Provider events ([Microsoft 365](/connectors/microsoft-live-events/),
  [Google Drive](/connectors/google-drive-live-events/)) lower change latency;
  the interval remains as reconciliation.
- Large deletions are confirmed across consecutive syncs before anything is
  tombstoned; see [Connectors in the product guide](/product/connectors/).

## Token lifetimes

Providers differ in how sessions persist, and the setup panel says so per
connector: most refresh silently; Microsoft's refresh tokens rotate on every
use, so losing one (for example by restoring an old backup) requires
re-authorizing the connection.
