---
title: Google Drive
description: Index shared drives and My Drive content with mirrored permissions, including Google Group expansion.
---

The Google Drive connector indexes **shared drives and My Drive files**,
mirroring Drive permissions — including expanding Google Group grants, so a
document shared with a group is visible to exactly its members.

| | |
| --- | --- |
| Syncs | Shared drives, files, My Drive files |
| Incremental | Drive Changes feed |
| Scoping | Shared drives, folders |
| Permission mirror | Yes — including Google Group expansion |
| Live events | [Workspace Events → Pub/Sub](/connectors/google-drive-live-events/) |
| Token type | Refresh token |

## Register the app in Google Cloud

In the [Google Cloud console](https://console.cloud.google.com/apis/credentials),
in the project that will own the app:

1. **OAuth consent screen:** set User type to **Internal**, so only accounts
   in the firm's Workspace can use it. Internal apps also skip Google's
   verification review, which the Drive scopes would otherwise require.
2. **Enable APIs:** APIs & Services → Library → **Google Drive API**,
   **Admin SDK API** — and, for [live events](/connectors/google-drive-live-events/),
   **Google Workspace Events API** and **Cloud Pub/Sub API** (optional until
   event delivery is configured).
3. **Create the client:** Credentials → Create credentials → OAuth client ID →
   Application type **Web application**. Add the appliance's callback (shown
   in the setup modal) under *Authorized redirect URIs*.
4. **Scopes:** OAuth consent screen → Data access → add:

   `https://www.googleapis.com/auth/drive.readonly`
   `https://www.googleapis.com/auth/drive.metadata.readonly`
   `https://www.googleapis.com/auth/admin.directory.group.readonly`
   `https://www.googleapis.com/auth/admin.directory.group.member.readonly`

   Google marks these "sensitive"/"restricted" — expected for a read-only
   document connector.
5. **Secret:** the client id (ends in `.apps.googleusercontent.com`) and
   secret are on the client's detail page.

## Grant the authorizing account directory read

In the Google **Admin console**, give the account that will authorize the
connection the least-privilege **Groups → Read API** admin privilege.
Without it, Google Group grants cannot be expanded and group-shared documents
stay invisible under fail-closed permissions. If the Workspace restricts
third-party API access, an admin must also mark the client id **Trusted**
(Security → API controls → App access control) or sign-in returns
`access_denied`.

That account also needs ordinary read access to the shared drives and folders
the appliance should index — directory read does not grant Drive content.

## Connect

Enter the client id and secret and authorize with a Workspace account in the
firm's domain (an Internal app rejects personal `gmail.com` accounts). Then
pick shared drives and folders in the scope picker.
