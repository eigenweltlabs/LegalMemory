"""A source's group names must not reach across sources.

Directory groups carry globally unique identifiers, so a tenant GUID means the same
thing wherever it is read. Most source groups do not: every SharePoint site names its
owners group "Owners", every Slack workspace has a "#general", and a firm running one
appliance across several client tenants would otherwise have one client's site owners
matching another client's documents on nothing more than a shared word.

These tests pin both halves — grants and memberships are scoped identically, so a
scoped grant still matches its own members — and pin what must *not* be scoped:
directory groups, which are meant to span sources, and grants an operator typed by hand,
which name principals in the appliance's own namespace.
"""

from __future__ import annotations

import pytest

from knowledge_index.connectors.principals import (
    expand_with_memberships,
    qualify_group_id,
    qualify_principal,
    replace_memberships,
)
from knowledge_index.db.models import Project, Source, SourceObject, SourceObjectGrant
from knowledge_index.permissions import replace_source_object_grants


# -- pure scoping rules ------------------------------------------------------


def test_site_group_names_differ_across_sources():
    a = qualify_principal("group:sp:owners", "source-a")
    b = qualify_principal("group:sp:owners", "source-b")
    assert a != b, "the same site group name must not collide across two clients"


def test_directory_groups_are_left_global():
    guid = "group:entra:9f2c1e40-0000-4a5b-9c3d-1122334455aa"
    assert qualify_principal(guid, "source-a") == qualify_principal(guid, "source-b") == guid


def test_users_are_never_scoped():
    email = "user:anwalt@kanzlei.de"
    assert qualify_principal(email, "source-a") == email


def test_scoping_is_idempotent():
    once = qualify_group_id("sp:owners", "source-a")
    assert qualify_group_id(once, "source-a") == once


def test_role_and_malformed_principals_pass_through():
    assert qualify_principal("role:authenticated", "source-a") == "role:authenticated"
    assert qualify_principal("group:", "source-a") == "group:"
    assert qualify_principal("", "source-a") == ""


def test_missing_source_id_leaves_the_principal_alone():
    # A caller with no source in hand must not invent a scope that will never match.
    assert qualify_principal("group:sp:owners", "") == "group:sp:owners"


# -- the grant side ----------------------------------------------------------


@pytest.fixture()
def two_sources(session):
    """Two SharePoint sources standing in for two client tenants on one appliance."""
    rows = []
    for suffix in ("a", "b"):
        project = Project(key=f"P-CLIENT-{suffix.upper()}", name=f"Client {suffix}")
        session.add(project)
        session.flush()
        source = Source(
            project_id=project.id,
            kind="sharepoint_online",
            display_name=f"Client {suffix} SharePoint",
            provider="native",
        )
        session.add(source)
        session.flush()
        obj = SourceObject(
            source_id=source.id,
            external_id=f"doc-{suffix}",
            path=f"/{suffix}/schriftsatz.docx",
            name="schriftsatz.docx",
        )
        session.add(obj)
        session.flush()
        rows.append((source, obj))
    return rows


def _principals(session, source_object_id: str) -> set[str]:
    return {
        row.principal
        for row in session.query(SourceObjectGrant).filter(
            SourceObjectGrant.source_object_id == source_object_id
        )
    }


def test_mirrored_grants_do_not_collide_across_clients(session, two_sources):
    (source_a, object_a), (source_b, object_b) = two_sources
    acl = [{"principal": "group:sp:owners", "effect": "allow", "origin": "connector"}]
    replace_source_object_grants(session, object_a.id, acl, source_id=source_a.id)
    replace_source_object_grants(session, object_b.id, acl, source_id=source_b.id)
    session.flush()

    granted_a = _principals(session, object_a.id)
    granted_b = _principals(session, object_b.id)
    assert granted_a and granted_b
    assert not (granted_a & granted_b), (
        f"client A's owners {granted_a} must not match client B's document {granted_b}"
    )


def test_operator_grants_are_stored_as_typed(session, two_sources):
    (source_a, object_a), _ = two_sources
    replace_source_object_grants(
        session,
        object_a.id,
        [{"principal": "group:litigation", "effect": "allow", "origin": "manual"}],
        source_id=source_a.id,
    )
    session.flush()
    assert _principals(session, object_a.id) == {"group:litigation"}


def test_deny_grants_are_scoped_too(session, two_sources):
    (source_a, object_a), (source_b, object_b) = two_sources
    acl = [{"principal": "group:sp:externals", "effect": "deny", "origin": "connector"}]
    replace_source_object_grants(session, object_a.id, acl, source_id=source_a.id)
    replace_source_object_grants(session, object_b.id, acl, source_id=source_b.id)
    session.flush()
    assert not (_principals(session, object_a.id) & _principals(session, object_b.id))


# -- the membership side, which must still meet the grant side ---------------


def test_a_scoped_grant_still_matches_its_own_members(session, two_sources):
    (source_a, object_a), (source_b, _) = two_sources
    replace_source_object_grants(
        session,
        object_a.id,
        [{"principal": "group:sp:owners", "effect": "allow", "origin": "connector"}],
        source_id=source_a.id,
    )
    replace_memberships(
        session,
        source_a.id,
        [{"group_id": "sp:owners", "member_id": "anwalt@kanzlei.de", "member_type": "user"}],
    )
    session.flush()

    expanded = expand_with_memberships(session, {"user:anwalt@kanzlei.de"})
    assert _principals(session, object_a.id) <= expanded, (
        "scoping both sides must not break the match it exists to make"
    )


def test_membership_in_one_source_does_not_grant_another(session, two_sources):
    (source_a, object_a), (source_b, object_b) = two_sources
    replace_source_object_grants(
        session,
        object_b.id,
        [{"principal": "group:sp:owners", "effect": "allow", "origin": "connector"}],
        source_id=source_b.id,
    )
    # The caller owns client A's site, and only client A's.
    replace_memberships(
        session,
        source_a.id,
        [{"group_id": "sp:owners", "member_id": "partner@kanzlei.de", "member_type": "user"}],
    )
    session.flush()

    expanded = expand_with_memberships(session, {"user:partner@kanzlei.de"})
    assert not (_principals(session, object_b.id) & expanded), (
        "owning client A's site must not open client B's documents"
    )


def test_nested_group_edges_are_scoped_on_both_sides(session, two_sources):
    (source_a, object_a), _ = two_sources
    replace_source_object_grants(
        session,
        object_a.id,
        [{"principal": "group:sp:owners", "effect": "allow", "origin": "connector"}],
        source_id=source_a.id,
    )
    replace_memberships(
        session,
        source_a.id,
        [
            {"group_id": "sp:owners", "member_id": "sp:partners", "member_type": "group"},
            {"group_id": "sp:partners", "member_id": "partner@kanzlei.de", "member_type": "user"},
        ],
    )
    session.flush()

    expanded = expand_with_memberships(session, {"user:partner@kanzlei.de"})
    assert _principals(session, object_a.id) <= expanded, (
        "a group nested inside a granted group must still resolve once both are scoped"
    )


def test_an_alias_matches_whichever_spelling_the_idp_used():
    """An operator maps an address; the IdP decides which principal kind carries it.

    OIDC puts the immutable subject in `user:` (a Keycloak UUID) and the email in
    `username:`. An alias written as `user:person@firm.de` therefore matched a shape no
    real token produces, and the person silently saw nothing — the failure this guards.
    """
    from knowledge_index.connectors.principals import apply_aliases

    aliases = {"user:me@example.com": "user:corp.user@tenant.example"}

    # As an OIDC token actually arrives: subject is a UUID, the email is the username.
    oidc = {"user:1d1b0c2e-uuid", "username:me@example.com", "role:authenticated"}
    assert "user:corp.user@tenant.example" in apply_aliases(oidc, aliases)

    # And the spelling the alias was literally written in still works.
    assert "user:corp.user@tenant.example" in apply_aliases({"user:me@example.com"}, aliases)

    # An unrelated caller gains nothing.
    assert apply_aliases({"username:other@example.com"}, aliases) == {"username:other@example.com"}
