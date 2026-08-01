---
title: Clio
description: Index matters and documents from Clio Manage, scoped by matter, with matter-visibility permissions mirrored.
---

The Clio connector indexes **matters and their documents** from Clio Manage
(API v4), mirroring matter visibility: restricted matters the authorizing user
cannot open are never fetched at all.

| | |
| --- | --- |
| Syncs | Matters (as containers), documents, document deletions |
| Incremental | `updated_since` cursor |
| Scoping | Matters as roots |
| Permission mirror | Yes |
| Token type | Refresh token |
| Region | EU (`eu.app.clio.com`) by default; per-connection override via the `api_base_url` field |

## Register the app in the Clio developer portal

In the [Clio developer portal](https://developers.clio.com):

1. **Region matters.** Register in the **same region** the appliance will read
   from — Clio runs isolated regions (US, EU, CA, AU) and tokens are
   region-bound. An app registered against the US instance cannot authorize an
   EU firm.
2. **Create the app:** Developer Apps → New App.
3. **Redirect URI:** enter the appliance's callback exactly as shown in the
   setup modal. Clio rejects `localhost` — use the `127.0.0.1` form.
4. **Permissions:** on the app, tick **Read** for *Documents, Matters, Users,
   Custom fields, Contacts* and *General* — and nothing else. Clio has no
   OAuth scope parameter; the checkboxes on the app are the grant. No Write
   permission is ever needed: the appliance mirrors, it must not be able to
   change who sees a document.
5. **Credentials:** the app page shows the Client ID ("App key") and Client
   Secret ("App secret") after saving.

## Connect

Enter the app key and secret and authorize with a **dedicated standard user
whose matter visibility is exactly what the firm wants searchable**. The
connection can never see more than that user.

After authorization, pick the matters to index in the scope picker.
