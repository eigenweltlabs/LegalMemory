---
title: OneDrive
description: Index a OneDrive for Business drive with mirrored sharing permissions.
---

The OneDrive connector indexes the files and folders in a person's
**OneDrive for Business drive** through Microsoft Graph, mirroring the drive's
sharing permissions.

| | |
| --- | --- |
| Syncs | Drive items (files and folders) |
| Incremental | Graph delta feed |
| Scoping | Folders |
| Permission mirror | Yes, owner-scoped, plus shares |
| Live events | [Azure Event Hubs](/connectors/microsoft-live-events/) |
| Token type | Refresh token |

OneDrive is a **personal-corpus connector**: everything defaults to visible to
the drive's owner. Granting a OneDrive connection to a group or role requires
explicit confirmation in the setup form, because it would publish one person's
entire drive.

## Register the app in Entra

Same registration as [SharePoint Online](/connectors/sharepoint-online/#register-the-app-in-entra):
single-tenant app, **Web** platform, redirect URI from the setup modal,
admin consent, secret **Value**. The delegated Graph scopes for OneDrive are:

`offline_access`, `User.Read`, `User.Read.All`, `Files.Read.All`,
`Group.Read.All`

`Group.Read.All` lets the permission mirror resolve group grants returned by
the drive's `/permissions`; `User.Read.All` resolves the id-only identities
Graph returns for app-created shares, which would otherwise mirror as opaque
GUIDs matching no caller.

## Connect

Enter the client id and secret, authorize as the drive's owner (work or school
account), then pick the folders to index in the scope picker.

## Notes

- The connector re-requests the full scope set on every token refresh, so a
  connection authorized before a scope was added gains it without manual
  re-authorization.
- Unlicensed "app folder only" drives are handled and reported honestly rather
  than synced as empty.
