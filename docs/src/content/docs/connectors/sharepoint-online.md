---
title: SharePoint Online
description: Index SharePoint sites, document libraries and site pages with mirrored permissions — including ethical-wall-grade group resolution.
---

The SharePoint Online connector indexes **sites → document libraries → files
and folders, plus site pages** through Microsoft Graph, mirroring SharePoint
permissions (users, Entra groups, sharing links) onto every document.

| | |
| --- | --- |
| Syncs | Document libraries (files + folders), site pages |
| Incremental | Graph delta feed per library |
| Scoping | Sites, libraries, folders |
| Permission mirror | Yes — including Entra group expansion |
| Live events | [Azure Event Hubs](/connectors/microsoft-live-events/) |
| Token type | Rotating refresh token |

## Register the app in Entra

One Entra app registration serves all Microsoft connectors; only the scopes
differ. In the [Microsoft Entra admin center](https://entra.microsoft.com/):

1. **Create the registration.** Identity → Applications → App registrations →
   New registration. Under *Supported account types* choose **single tenant** —
   the app is only ever used by this firm.
2. **Redirect URI.** Add the appliance's callback (shown in the setup modal;
   it is `<KI_PUBLIC_BASE_URL>/api/connectors/oauth/callback`) with platform
   type **Web** — not *Single-page application*, which refuses a client secret.
3. **Delegated Graph permissions.** API permissions → Add a permission →
   Microsoft Graph → *Delegated*:

   `offline_access`, `User.Read.All`, `Group.Read.All`,
   `GroupMember.Read.All`, `Directory.Read.All`, `Sites.Read.All`,
   `Files.Read.All`

   The three group/directory scopes are what make **ethical walls** work:
   without them Entra group memberships cannot be expanded and every
   group-shared document stays invisible under fail-closed permissions.
4. **Grant admin consent.** Press *Grant admin consent for &lt;tenant&gt;* on
   the API permissions page. This is the step most registrations miss — until
   a tenant administrator presses it, every permission row reads "Not granted"
   and sign-in either fails or returns an empty estate.
5. **Client secret.** Certificates & secrets → New client secret. Copy the
   **Value** column, not *Secret ID* — Value is shown once. The client id is
   on Overview as *Application (client) ID*.

## Connect

In **Connectors → SharePoint Online**, enter the client id and secret and
authorize. Sign in with a **work or school account** that can see the estate
you intend to index — the appliance authorizes against Microsoft's
`/organizations` endpoint, which rejects personal Microsoft accounts, and the
connection sees exactly what that account sees.

After authorization, the scope picker browses your sites and libraries; select
the subtrees to index.

## Options

- **Sensitivity labels (Purview):** exclude documents by label GUID
  (`excluded_sensitivity_label_ids`), skip encrypted files, or skip unlabeled
  files. Sublabels must be listed explicitly.

## Notes

- Microsoft rotates the refresh token on every refresh; if a rotated token is
  lost (for example after a restore from an old backup), re-authorize the
  connection from its detail drawer.
- Permission-only changes do not alter file etags; the periodic full scan
  (`security.acl_refresh_hours`, default 24h) is what picks up revocations.
