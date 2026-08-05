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

## Register the app in the Dropbox App Console

Create the app from the account that owns the estate, at
[dropbox.com/developers/apps](https://www.dropbox.com/developers/apps):

1. **Create app** → **Scoped access** → access type **Full Dropbox**. An
   App-folder app can only ever see one directory it created for itself.
2. **Permissions** tab: tick `files.metadata.read`, `files.content.read` and
   `sharing.read`. For group expansion also tick `groups.read`. Press
   **Submit** at the bottom — that is a separate step from ticking the boxes.
3. **Settings** → **OAuth 2** → **Redirect URIs**: add the URI shown in the
   setup modal.
4. **Settings** → **App key and App secret**: the App key is the client id, the
   App secret is the client secret.

On a Dropbox Business team a team admin has to approve the app before it can be
installed.

Permissions ticked *after* a token was issued do not apply to that token.
Change the Permissions tab first, then authorize.

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
