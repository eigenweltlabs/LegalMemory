---
title: Dropbox
description: Index a Dropbox account with folder-level sharing mirrored into the index, incremental sync from Dropbox's listing cursors, and Dropbox groups expanded into their members.
---

The Dropbox connector indexes the files in a Dropbox account, mirroring who may
open each one. Firms that use Dropbox as a file server share at the **folder**
level, so that is what the permission mirror is built around: a shared folder's
members are read once and applied to everything inside it.

| | |
| --- | --- |
| Syncs | Files (folders are traversed, not indexed) |
| Incremental | `files/list_folder` cursors |
| Scoping | Folders |
| Permission mirror | Yes, per shared folder, plus per-file shares |
| Group expansion | Dropbox Business team groups |
| Token type | Refresh token |
| Team support | Dropbox Business team tokens, indexing the shared team space |

## Register the app in the Dropbox App Console

Create the app from the account that owns the estate, at
[dropbox.com/developers/apps](https://www.dropbox.com/developers/apps):

1. **Create app** → **Scoped access** → access type **Full Dropbox**. An
   App-folder app can only ever see one directory it created for itself.
2. **Permissions** tab: tick `account_info.read`, `files.metadata.read`,
   `files.content.read` and `sharing.read`. On a Dropbox Business team — the
   normal case for a firm — also tick the team scopes: `team_info.read`,
   `team_data.member`, `team_data.content.read`, `files.team_metadata.read`,
   `members.read` and `groups.read`. Press **Submit** at the bottom — that is
   a separate step from ticking the boxes.

   `account_info.read` is not optional: every sync opens with
   `users/get_current_account`, both to validate the credential and to learn
   the owner's address, which is what makes an unshared file readable by the
   account that authorized the connection. Without it the first call fails and
   the connection looks dead rather than under-scoped.

   The OpenID scopes (`openid`, `profile`, `email`) cannot coexist with team
   scopes — the console says so itself. Leave them off; this connector does
   not use them.
3. **Settings** → **OAuth 2** → **Redirect URIs**: add the URI shown in the
   setup modal.
4. **Settings** → **App key and App secret**: the App key is the client id, the
   App secret is the client secret.

Permissions ticked *after* a token was issued do not apply to that token.
Change the Permissions tab first, then authorize.

## Dropbox Business teams

Dropbox has two kinds of credential, and which one an app produces is decided
by its scopes, not by anything this appliance sends: an app with team scopes
authorized by a team admin yields a **team token**, anything else a **user
token**. They are not interchangeable. A user token is refused by every
`/2/team/*` route regardless of what was ticked, and a team token cannot touch
a file until it names the team member it acts as. The connector probes
`team/get_info` once per run and handles both:

- **Team token.** The connection acts as the admin who authorized it (or the
  member named in **Act As Member**), resolved through
  `team/token/get_authenticated_admin` — and reads the team's shared space,
  the team folders a firm actually works out of, by pointing every file
  request at the team's root namespace. Group expansion uses the team
  directory natively.
- **User token.** Exactly the behaviour described elsewhere on this page: the
  authorizing account's own estate, group expansion only as far as the token
  can reach.

Two things follow for a team app:

- The **Generate access token** button disappears from the App Console once
  team scopes are ticked. A team token can only come from the OAuth flow,
  authorized by a **team admin**; authorize from the setup modal as usual.
- After the admin authorizes, the app's **Development teams** counter on the
  Settings tab should read 1/5. At 0/5 the team never linked, and every
  `/2/team/*` call will be refused no matter the scopes.

Re-authorizing a connection with the other kind of token, changing the acting
member, or toggling the team space all cause the next sync to crawl rather
than resume: the stored change cursors describe an estate the new identity is
not looking at.

## Connect

Enter the client id and secret, authorize as the account whose Dropbox should
be indexed, then pick the folders to index in the scope picker. An empty
selection syncs the whole account.

### Configuration

| Option | Default | Effect |
| --- | --- | --- |
| Exclude Path | empty | Path fragment to skip, for example `/archiv`. Matched case-insensitively against the lowercase path. |
| Mirror Sharing Members | on | Read sharing members and mirror them as grants. Off leaves permissions unknown, and documents stay invisible until an administrator grants access at project level. |
| Expand Dropbox Groups | on | Resolve Dropbox group members through the team API, so a folder shared with a group is reachable by the people in it. |
| Index The Team Space | on | With a team token, index the team's shared space — the team folders — rather than the home directory of the member the token acts as. Ignored by a user token. |
| Act As Member | empty | The team member whose access a team token uses, by email address. Blank acts as the admin who authorized the token, with admin reach over the team space; a named member reaches what that member can reach. Ignored by a user token. |

## How permissions are resolved

Each file resolves to one of three shapes, in the order Dropbox itself applies
them:

1. **Shared individually** (`has_explicit_shared_members`) — the file's own
   members are read with `sharing/list_file_members` and used as-is. A wider
   membership on the parent folder does not leak onto it.
2. **Inside a shared folder** — the members of that shared folder are read once
   with `sharing/list_folder_members` and reused for every file inside it. A
   matter folder with four hundred documents costs one request, not four
   hundred.
3. **Neither** — the file is private to the account, and the authorizing
   account is its viewer.

Four rules apply throughout:

- **Known access levels only.** Dropbox's `AccessLevel` has four values —
  `owner`, `editor`, `viewer`, `viewer_no_comment` — and all four confer read.
  The union is open, so a level Dropbox adds later arrives as a tag this
  connector has not been told the meaning of; those are not mirrored. The cost
  is that a genuinely new read level goes unmirrored until the connector learns
  it, which is the safe direction to be wrong in.
- **An invitation is not access.** Outstanding invitees are never mirrored.
- **Only active teammates count.** A group lists its members whatever their
  standing with the team. Members who are `invited` (not yet joined),
  `suspended` or `removed` cannot open anything and are not mirrored, so a
  departed colleague does not keep reading through a group they were never
  removed from.
- **Unknown is not empty.** A members read that fails leaves the file's
  permissions *unknown*, which fails closed and is reported as a capability
  gap. An empty grant list would instead assert that nobody may read the file,
  which is a different claim.

### Groups

A file shared with a Dropbox group is granted to `group:dropbox:<group_id>`.
Nobody signs in to this appliance as a Dropbox group id, so those groups are
expanded into their members through `team/groups/members/list` and mirrored as
memberships. That expansion is what makes a group-shared matter folder
reachable by the group rather than by nobody.

The team API is a **Dropbox Business** feature. On a personal account the first
call is refused, expansion stops for the run, and group grants stay unmatched —
the sync itself is unaffected, and members named directly on the folder still
reach their documents. To bridge a Dropbox group onto a group the firm already
manages in its identity provider, use `security.principal_aliases`.

## Sync behaviour

The first sync crawls each synced root with a single recursive
`files/list_folder` call and stores the cursor for the next run. Later syncs
drain `files/list_folder/continue`.

Four behaviours are worth knowing when reading a sync report:

- **The change cursor is minted before the crawl starts**, with
  `files/list_folder/get_latest_cursor`. A file written while the crawl is
  running is therefore replayed by the next incremental drain instead of being
  missed until the following full scan.
- **Permission changes do not appear in the change feed.** Adding somebody to a
  shared folder rewrites no file, so no entry is produced for it. Permissions
  are re-read on the periodic full crawl, which the appliance forces at least
  every `security.acl_refresh_hours` (default 24) for exactly this reason.
  Group *membership* changes are picked up on every sync, including incremental
  ones.
- **Deletions are resolved from a path map.** Dropbox reports a removal as a
  path with no file id, while the index is keyed by file id, so the connector
  records which id it indexed at each path. Deleting a folder is not guaranteed
  to produce an entry per child, so the removal of a folder tombstones every
  indexed path beneath it. Past 50,000 tracked paths the map is traded for a
  full crawl, which tombstones by diff instead.
- **A rename is not a deletion.** Dropbox reports it as a removal at the old
  path plus the file at its new one, with the same id on both sides; the
  removal is suppressed and the document keeps its identity and its history.

A cursor Dropbox rejects, a change in the folder selection, and an interrupted
crawl all cause the next sync to crawl rather than resume.

## Notes

- Files with no downloadable bytes — Dropbox Paper documents and other
  server-side formats — are skipped rather than indexed empty.
- Downloads are pinned to the revision the listing reported, so a document
  saved over mid-crawl is not staged under the previous revision's token.
- The `rev` of a file is used as its version token, so a rescan costs one
  metadata pass plus downloads for genuinely changed files.
