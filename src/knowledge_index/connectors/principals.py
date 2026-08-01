"""Bridge source identities onto this appliance's identities.

A source and the appliance name the same person differently. SharePoint reports viewers
as ``user:anwalt@kanzlei.de`` and ``group:entra:<guid>``; the appliance authenticates
callers as ``user:<oidc-subject>``, ``username:<preferred-username>`` and
``group:<keycloak-group>``. Those namespaces do not overlap, so a mirrored ACL matches
nothing until something reconciles them — and because the permission compiler is
fail-closed, "matches nothing" means the firm's documents are invisible rather than
over-shared.

Two mechanisms, in order of preference:

**Group membership expansion** (``expand_with_memberships``) — the general answer. The
connector mirrors who belongs to each source group, so a caller identified by email is
recognised as a member of ``group:entra:<guid>`` without anyone maintaining a mapping.

**An explicit alias map** (``SecurityConfig.principal_aliases``) — the escape hatch, for
sources that cannot report memberships and for pinning a source group to a Keycloak
group the firm already manages.

Everything is casefolded. A grant that fails to match because of letter case is
indistinguishable from a deliberate denial, and silently invisible documents are the
hardest class of bug to notice here.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_index.db.models import SourceGroupMember


def normalize(principal: str) -> str:
    return str(principal).strip().casefold()


# Some group identifiers are unique across every tenant in the world — a directory GUID
# is one. Most are not: a SharePoint site names its owners group "Owners", and so does
# every other site at every other firm. Left as read, ``group:sp:owners`` would grant one
# client's site owners access to another client's documents through nothing more than a
# shared English word. Anything outside this set is bound to the source it came from
# before it is stored or matched.
GLOBALLY_UNIQUE_GROUP_NAMESPACES = frozenset({"entra", "google"})
SOURCE_SCOPE_PREFIX = "src."


def qualify_group_id(group_id: str, source_id: str) -> str:
    """Bind a group identifier to the source that reported it, unless it is global."""
    cleaned = normalize(group_id)
    if not cleaned or not source_id:
        return cleaned
    if cleaned.partition(":")[0] in GLOBALLY_UNIQUE_GROUP_NAMESPACES:
        return cleaned
    if cleaned.startswith(SOURCE_SCOPE_PREFIX):
        return cleaned  # already bound
    return f"{SOURCE_SCOPE_PREFIX}{source_id}.{cleaned}"


def qualify_principal(principal: str, source_id: str) -> str:
    """Bind the group half of a ``group:`` principal to its source; pass the rest through.

    Users are named by email or directory subject, which are unambiguous across sources
    already, so only groups need scoping.
    """
    cleaned = normalize(principal)
    kind, separator, rest = cleaned.partition(":")
    if kind != "group" or not separator or not rest:
        return cleaned
    return f"group:{qualify_group_id(rest, source_id)}"


def apply_aliases(principals: set[str], aliases: dict[str, str] | None) -> set[str]:
    """Add configured aliases for the principals a caller already holds.

    Aliases are additive and never remove a principal: the map is a bridge between
    namespaces, not an access-control decision of its own.
    """
    if not aliases:
        return set(principals)
    normalized = {normalize(key): normalize(value) for key, value in aliases.items()}
    # An identity provider decides whether a person's email arrives as `user:` or
    # `username:` — OIDC puts the immutable subject in `user:` (a Keycloak UUID) and the
    # email in `username:`. An operator writing "this address is that person" cannot know
    # which, and an alias that matches only one silently grants nothing. So match on the
    # address and carry the alias across both spellings.
    interchangeable = ("user", "username")
    by_value: dict[str, str] = {}
    for key, value in normalized.items():
        kind, _, rest = key.partition(":")
        by_value[key] = value
        if kind in interchangeable and rest:
            for other in interchangeable:
                by_value.setdefault(f"{other}:{rest}", value)
    expanded = set(principals)
    for principal in principals:
        alias = by_value.get(principal)
        if not alias:
            continue
        expanded.add(alias)
        # The target has the same ambiguity: a mirrored ACL names people by email, which
        # arrives as `user:` here, so add the sibling spelling of the target too.
        kind, _, rest = alias.partition(":")
        if kind in interchangeable and rest:
            expanded.update(f"{other}:{rest}" for other in interchangeable)
    # Aliases are also useful in reverse: a firm that maps its Keycloak group onto a
    # source group should match documents granted to either.
    reverse: dict[str, set[str]] = {}
    for key, value in normalized.items():
        reverse.setdefault(value, set()).add(key)
    for principal in list(expanded):
        expanded |= reverse.get(principal, set())
    return expanded


def expand_with_memberships(session: Session, principals: set[str]) -> set[str]:
    """Add the source groups a caller belongs to, per mirrored memberships.

    Matches on the identifiers a caller can plausibly be known by at the source — the
    OIDC subject, the username and any email-shaped principal — because a source reports
    members by email while the appliance authenticates by subject.
    """
    candidates = {normalize(item) for item in principals}
    lookups: set[str] = set()
    for principal in candidates:
        kind, _, value = principal.partition(":")
        if kind in {"user", "username"} and value:
            lookups.add(value)
        elif kind == "group" and value:
            # A source group can be a member of another source group.
            lookups.add(value)
    if not lookups:
        return candidates

    rows = session.scalars(
        select(SourceGroupMember).where(SourceGroupMember.member_id.in_(sorted(lookups)))
    ).all()

    expanded = set(candidates)
    # Walk transitively: group-in-group edges mean one pass can miss an outer group.
    frontier = {row.group_id for row in rows}
    expanded |= {f"group:{group_id}" for group_id in frontier}
    seen: set[str] = set(frontier)
    while frontier:
        parents = session.scalars(
            select(SourceGroupMember).where(
                SourceGroupMember.member_type == "group",
                SourceGroupMember.member_id.in_(sorted(frontier)),
            )
        ).all()
        frontier = {row.group_id for row in parents if row.group_id not in seen}
        seen |= frontier
        expanded |= {f"group:{group_id}" for group_id in frontier}
    return expanded


def replace_memberships(session: Session, source_id: str, memberships: list[dict]) -> int:
    """Replace the mirrored memberships for one source.

    Replaced rather than merged: a member removed from a group at source must lose the
    grant, and a merge would leave the old edge in place — which is precisely the
    over-permission this table exists to prevent.
    """
    session.query(SourceGroupMember).filter(SourceGroupMember.source_id == source_id).delete()
    written = 0
    seen: set[tuple[str, str]] = set()
    for membership in memberships:
        # Scoped on the way in exactly as the grants are, so the two sides still meet.
        group_id = qualify_group_id(membership.get("group_id", ""), source_id)
        member_type = normalize(membership.get("member_type", "user")) or "user"
        member_id = normalize(membership.get("member_id", ""))
        if member_type == "group":
            # A group-in-group edge names a group on both sides of the edge.
            member_id = qualify_group_id(member_id, source_id)
        if not group_id or not member_id or (group_id, member_id) in seen:
            continue
        seen.add((group_id, member_id))
        session.add(
            SourceGroupMember(
                source_id=source_id,
                group_id=group_id,
                member_id=member_id,
                member_type=member_type,
                group_name=membership.get("group_name"),
            )
        )
        written += 1
    session.flush()
    return written
