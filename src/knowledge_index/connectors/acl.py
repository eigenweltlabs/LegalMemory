"""Turn a source's own permission payloads into principals.

Every source describes "who can see this" differently — Graph returns
``grantedToV2`` identity sets, Google returns a permissions list, Dropbox returns
sharing members, Confluence returns space and content restrictions. This module holds
the per-source translations so each connector's traversal code stays about traversal.

Two rules apply everywhere and both exist because getting them wrong is worse than
returning nothing:

**Read access only.** A principal appears only if its role actually confers read. A
"can request access" or pending invitation is not access.

**Unknown means unknown.** If a permission payload cannot be resolved to a principal it
is dropped, and if *no* payload could be read the connector returns ``None`` rather than
an empty list. ``None`` is fail-closed and flagged as a capability gap; ``[]`` asserts
"nobody may see this", and silently converting the first into the second would hide a
firm's corpus while looking like a deliberate restriction.

Principal namespaces are the source's own — ``user:{email}``, ``group:entra:{guid}``,
``group:google:{email}``. Reconciling them with this appliance's identities happens in
:mod:`knowledge_index.connectors.principals`.
"""

from __future__ import annotations

from typing import Any

from knowledge_index.connectors.entities._base import AccessControl

# Graph roles that confer read. "owner" and "member" appear on site/group permissions.
GRAPH_READ_ROLES = frozenset({"read", "write", "owner", "member", "sp.read", "sp.write"})

# Google Drive roles that confer read. "commenter" can read; "organizer" is shared-drive
# administration, which implies it too.
DRIVE_READ_ROLES = frozenset({"reader", "commenter", "writer", "fileOrganizer", "organizer", "owner"})


def _clean(value: object) -> str:
    return str(value or "").strip().casefold()


# --------------------------------------------------------------------- Microsoft Graph


def graph_principal(identity_set: dict[str, Any]) -> str | None:
    """Resolve one Graph ``identitySet`` to a principal.

    Email is preferred over object id because a firm's callers authenticate by email or
    UPN far more often than by directory GUID, so an email-shaped principal has a real
    chance of matching without an explicit alias.
    """
    user = identity_set.get("user") or {}
    if user:
        for key in ("email", "userPrincipalName", "loginName"):
            value = _clean(user.get(key))
            if value and "@" in value:
                return f"user:{value}"
        identifier = _clean(user.get("id"))
        if identifier:
            return f"user:id:{identifier}"

    group = identity_set.get("group") or {}
    if group:
        identifier = _clean(group.get("id"))
        if identifier:
            return f"group:entra:{identifier}"
        email = _clean(group.get("email"))
        if email:
            return f"group:entra:{email}"

    site_group = identity_set.get("siteGroup") or {}
    if site_group:
        label = _clean(site_group.get("loginName")) or _clean(site_group.get("displayName"))
        if label:
            return f"group:sp:{label.replace(' ', '_')}"

    application = identity_set.get("application") or {}
    if application:
        # Service principals are not people. Indexing their grant would let an app
        # registration's access widen what a human can retrieve.
        return None
    return None


def graph_permissions_to_access(permissions: list[dict[str, Any]]) -> AccessControl | None:
    """Translate a Graph ``/permissions`` collection (drive item, site, list).

    Returns ``None`` when the collection could not be read at all — the caller passes
    ``None`` through so the object stays fail-closed and is reported as a gap rather
    than as "nobody may read this".
    """
    if permissions is None:
        return None
    viewers: list[str] = []
    is_public = False
    for permission in permissions:
        roles = {_clean(role) for role in (permission.get("roles") or [])}
        link = permission.get("link") or {}
        if link:
            scope = _clean(link.get("scope"))
            if scope == "anonymous":
                # An anonymous sharing link means the document is readable without
                # authenticating. Mirroring that as "everyone here" would be wrong and
                # dangerous: it is an exposure to flag, not a grant to honour.
                continue
            if scope == "organization":
                is_public = True
                continue
        if roles and not (roles & GRAPH_READ_ROLES):
            continue
        granted = permission.get("grantedToV2") or permission.get("grantedTo") or {}
        principal = graph_principal(granted) if granted else None
        if principal:
            viewers.append(principal)
        for identity_set in permission.get("grantedToIdentitiesV2") or permission.get(
            "grantedToIdentities"
        ) or []:
            nested = graph_principal(identity_set)
            if nested:
                viewers.append(nested)
    return AccessControl(viewers=sorted(set(viewers)), is_public=is_public)


# ----------------------------------------------------------------------- Google Drive


def drive_permissions_to_access(permissions: list[dict[str, Any]]) -> AccessControl | None:
    """Translate a Google Drive ``permissions`` list."""
    if permissions is None:
        return None
    viewers: list[str] = []
    is_public = False
    for permission in permissions:
        if _clean(permission.get("role")) not in {role.casefold() for role in DRIVE_READ_ROLES}:
            continue
        if permission.get("pendingOwner") or permission.get("deleted"):
            continue
        kind = _clean(permission.get("type"))
        email = _clean(permission.get("emailAddress"))
        if kind == "user" and email:
            viewers.append(f"user:{email}")
        elif kind == "group" and email:
            viewers.append(f"group:google:{email}")
        elif kind == "domain":
            # Domain-wide inside the firm's own Workspace tenant: everyone who can
            # authenticate here.
            is_public = True
        elif kind == "anyone":
            # Link-shared to the public. Not a grant to mirror.
            continue
    return AccessControl(viewers=sorted(set(viewers)), is_public=is_public)


# ---------------------------------------------------------------------------- Dropbox


def dropbox_members_to_access(
    users: list[dict[str, Any]] | None,
    groups: list[dict[str, Any]] | None,
    invitees: list[dict[str, Any]] | None = None,
) -> AccessControl | None:
    """Translate Dropbox ``sharing/list_file_members`` output.

    Invitees are ignored: an outstanding invitation is not access, and mirroring it
    would grant on the strength of an email someone typed.
    """
    if users is None and groups is None:
        return None
    viewers: list[str] = []
    for member in users or []:
        email = _clean((member.get("user") or {}).get("email"))
        if email and _clean(member.get("access_type", {}).get(".tag") or "viewer") != "no_access":
            viewers.append(f"user:{email}")
    for member in groups or []:
        group = member.get("group") or {}
        identifier = _clean(group.get("group_id")) or _clean(group.get("group_name"))
        if identifier:
            viewers.append(f"group:dropbox:{identifier}")
    del invitees
    return AccessControl(viewers=sorted(set(viewers)), is_public=False)


# -------------------------------------------------------------------------------- Box


def box_collaborations_to_access(entries: list[dict[str, Any]] | None) -> AccessControl | None:
    """Translate a Box ``collaborations`` collection."""
    if entries is None:
        return None
    viewers: list[str] = []
    for entry in entries:
        if _clean(entry.get("status")) not in {"", "accepted"}:
            continue
        if _clean(entry.get("role")) in {"", "uploader", "previewer_uploader"}:
            # Upload-only roles cannot read existing content.
            continue
        accessible = entry.get("accessible_by") or {}
        kind = _clean(accessible.get("type"))
        email = _clean(accessible.get("login"))
        name = _clean(accessible.get("name"))
        if kind == "user" and email:
            viewers.append(f"user:{email}")
        elif kind == "group" and (accessible.get("id") or name):
            viewers.append(f"group:box:{_clean(accessible.get('id')) or name}")
    return AccessControl(viewers=sorted(set(viewers)), is_public=False)


# ------------------------------------------------------------------------- Confluence


def confluence_restrictions_to_access(
    restrictions: dict[str, Any] | None, *, space_is_open: bool = True
) -> AccessControl | None:
    """Translate Confluence read restrictions on a page.

    Confluence inverts the usual model: with no restrictions, a page is readable by
    anyone who can see the space. So an empty restriction set means "space-wide", not
    "nobody" — collapsing that into an empty viewer list would black out an entire wiki.
    """
    if restrictions is None:
        return None
    read = ((restrictions.get("read") or {}).get("restrictions")) or {}
    users = [
        _clean(user.get("email") or user.get("username") or user.get("accountId"))
        for user in ((read.get("user") or {}).get("results") or [])
    ]
    groups = [
        _clean(group.get("name") or group.get("id"))
        for group in ((read.get("group") or {}).get("results") or [])
    ]
    viewers = [f"user:{user}" for user in users if user] + [
        f"group:confluence:{group}" for group in groups if group
    ]
    if not viewers:
        return AccessControl(viewers=[], is_public=space_is_open)
    return AccessControl(viewers=sorted(set(viewers)), is_public=False)


# ------------------------------------------------------------------------------ Teams


def teams_members_to_access(
    members: list[dict[str, Any]] | None, *, team_group_id: str | None = None
) -> AccessControl | None:
    """Translate a Teams channel's membership into read principals.

    A channel message is visible to the channel's members. For a standard channel that is
    the whole team, so the backing group is emitted and expanded through mirrored
    memberships; a private channel lists its own members.
    """
    if members is None and not team_group_id:
        return None
    viewers: list[str] = []
    for member in members or []:
        email = _clean(member.get("email"))
        if email:
            viewers.append(f"user:{email}")
            continue
        identifier = _clean(member.get("userId") or member.get("id"))
        if identifier:
            viewers.append(f"user:id:{identifier}")
    if team_group_id:
        viewers.append(f"group:entra:{_clean(team_group_id)}")
    return AccessControl(viewers=sorted(set(viewers)), is_public=False)


# --------------------------------------------------------- Owner-scoped corpora
#
# A mailbox, a calendar and a notebook are not objects with permission lists; each is one
# person's own material. That single owner is a real access control, not the absence of
# one, so these sources mirror it instead of leaving every item unknown — which would hide
# a connected mailbox completely.


def owner_to_access(owner_email: str | None) -> AccessControl | None:
    """The ACL of a corpus that belongs to exactly one person.

    ``None`` when the owner could not be resolved: unknown, fail-closed, and reported as a
    capability gap. Returning an empty ``AccessControl`` instead would assert that nobody
    may read a mailbox its owner is actively using.
    """
    email = _clean(owner_email)
    if not email:
        return None
    return AccessControl(viewers=[f"user:{email}"], is_public=False)


def graph_owner_email(me: dict[str, Any] | None) -> str | None:
    """Pull the signed-in account's address out of a Graph ``/me`` payload.

    ``mail`` is preferred and ``userPrincipalName`` is the fallback: a UPN is not
    guaranteed to be routable mail, but it is what a caller authenticates as, so it is a
    better principal than nothing. Anything without an ``@`` is not an address and is
    dropped rather than mirrored as one.
    """
    if not me:
        return None
    for key in ("mail", "userPrincipalName"):
        value = _clean(me.get(key))
        if value and "@" in value:
            return value
    return None


# ------------------------------------------------------------------------------ Slack


def slack_channel_to_access(
    *,
    is_private: bool,
    member_ids: list[str] | None = None,
    emails_by_user_id: dict[str, str] | None = None,
) -> AccessControl | None:
    """Translate a Slack channel's membership into read principals.

    A public channel is readable by every member of the workspace, including people who
    have not joined it, so it maps to ``is_public`` rather than to an enumerated viewer
    list. Enumerating the workspace would be both large and stale the moment somebody is
    hired.

    A private channel is readable by its members only. Slack identifies them by opaque
    user id, which nobody authenticates to this appliance as, so each id is resolved to
    an email through the cached ``users.list`` directory. If the membership could not be
    read, or none of it resolved, the result is ``None`` — unknown, fail-closed, and
    reported as a capability gap. An empty ``AccessControl`` would instead assert that a
    channel the firm is actively using may be read by nobody, which is a different and
    wrong claim.
    """
    if not is_private:
        return AccessControl(viewers=[], is_public=True)
    if member_ids is None or emails_by_user_id is None:
        return None
    viewers = [
        f"user:{email}"
        for email in (_clean(emails_by_user_id.get(member_id)) for member_id in member_ids)
        if email
    ]
    if not viewers:
        return None
    return AccessControl(viewers=sorted(set(viewers)), is_public=False)
