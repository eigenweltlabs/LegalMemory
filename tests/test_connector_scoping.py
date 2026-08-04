"""Parsing and fingerprinting of a connector's folder selection.

The fingerprint decides whether the next sync is treated as a deliberate re-scope — which
is allowed to delete documents — or as a possibly-broken scan, which is not. So what does
and does not count as a change is a security-relevant question, not a cosmetic one.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.connectors import scoping
from knowledge_index.connectors.registry import CATALOG
from knowledge_index.db.models import Source
from knowledge_index.web.app import _config_fields, create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}


def test_no_selection_means_the_whole_source():
    assert scoping.parse_roots(None) == []
    assert scoping.to_node_selections({}) == []
    assert scoping.describe(None)["scoped"] is False
    assert scoping.describe(None)["decided"] is False


def test_roots_accept_bare_ids_and_full_objects():
    roots = scoping.parse_roots({"roots": ["abc", {"id": "def", "title": "Mandate/2026"}]})
    assert [root["id"] for root in roots] == ["abc", "def"]
    # A bare id still gets a usable title, so the UI never renders an empty row.
    assert roots[0]["title"] == "abc"
    assert roots[1]["title"] == "Mandate/2026"


def test_duplicate_and_empty_roots_are_dropped():
    roots = scoping.parse_roots({"roots": [{"id": "a"}, {"id": "a"}, {"id": ""}, None, 7]})
    assert [root["id"] for root in roots] == ["a"]


def test_selection_converts_to_what_a_connector_traversal_expects():
    (selection,) = scoping.to_node_selections(
        {"roots": [{"id": "abc", "type": "folder", "title": "Mandate"}]}
    )
    assert selection.source_node_id == "abc"
    assert selection.node_type == "folder"
    assert selection.node_title == "Mandate"


def test_fingerprint_ignores_order_and_presentation():
    # Reordering in the UI, or a provider renaming a folder, must not read as a re-scope:
    # that would force a full rebuild of the firm's index for nothing.
    first = scoping.fingerprint({"roots": [{"id": "a", "title": "Mandate"}, {"id": "b"}]})
    second = scoping.fingerprint({"roots": [{"id": "b"}, {"id": "a", "title": "Matters"}]})
    assert first == second


def test_fingerprint_changes_when_a_root_is_added_or_removed():
    # These are the changes that must be recognised, because documents enter or leave the
    # index as a result.
    narrow = scoping.fingerprint({"roots": [{"id": "a"}]})
    wide = scoping.fingerprint({"roots": [{"id": "a"}, {"id": "b"}]})
    assert narrow != wide
    assert scoping.fingerprint({"roots": []}) != narrow


def test_describe_reports_what_the_admin_ui_shows():
    described = scoping.describe({"roots": [{"id": "a", "title": "Mandate/2026"}]})
    assert described["scoped"] is True
    assert described["root_count"] == 1
    assert described["roots"][0]["title"] == "Mandate/2026"
    assert described["fingerprint"]


def test_only_folder_shaped_sources_advertise_scoping():
    """A mailbox or chat workspace has no folder tree worth scoping.

    Advertising it would put a folder picker in front of an operator that cannot work.
    """
    scoped = {spec.short_name for spec in CATALOG if spec.supports_scoping}
    assert scoped == {
        "sharepoint_online",
        "onedrive",
        "google_drive",
        "dropbox",
        "box",
        "clio",
        # NetDocuments picks by cabinet: flat, like Clio's matter list, but a real
        # unit of selection — and the one a firm draws its walls around.
        "netdocuments",
    }


# ---------------------------------------------------------------------------
# What the connect form asks for
# ---------------------------------------------------------------------------


def _deferred(short_name: str) -> set[str]:
    spec = next(entry for entry in CATALOG if entry.short_name == short_name)
    return {
        field["name"]
        for field in _config_fields(spec)
        if field.get("superseded_by") == "folder_picker"
    }


def test_the_connect_form_does_not_ask_for_what_the_picker_answers():
    """Typing folder paths at setup time is a guess and nothing more.

    The connect form runs before the provider has authorized anything, so the appliance
    has never seen the drive: an operator asked for "Include Patterns" there can only
    write down what they remember. Those fields are deferred to the tree picker, which
    runs against the real folders. This is the exact set, per connector, because a
    connector that gains a setting must not silently lose its control.
    """
    assert _deferred("google_drive") == {"include_patterns"}
    assert _deferred("dropbox") == {"exclude_path"}
    assert _deferred("box") == {"folder_id"}
    assert _deferred("sharepoint_online") == {"site_url"}
    assert _deferred("onedrive") == set()


def test_a_setting_that_is_not_a_place_is_still_asked_for():
    """The picker replaces "where", not "what".

    A Purview label filter mentions sites in passing and a checkbox is answerable
    without seeing anything, so both stay on the form. Deferring them would silently
    remove a control an operator needs.
    """
    asked = {
        field["name"]: field
        for field in _config_fields(next(s for s in CATALOG if s.short_name == "sharepoint_online"))
        if not field.get("superseded_by")
    }
    assert "excluded_sensitivity_label_ids" in asked
    assert "include_personal_sites" in asked and "skip_encrypted_files" in asked


def test_a_connector_without_a_picker_keeps_its_folder_fields():
    """Outlook's `included_folders` are mail folders: there is no tree to browse.

    Deferring them to a picker that does not exist would leave the setting unreachable.
    """
    assert _deferred("outlook_mail") == set()
    assert _deferred("gmail") == set()


def test_a_deferred_field_still_travels_with_its_default():
    """Deferring is a UI decision, not a change to what the connector receives.

    The form submits every field's schema default whether or not it renders a control,
    so a connector behaves exactly as it did before its field was hidden.
    """
    fields = {field["name"]: field for field in _config_fields(_spec("google_drive"))}
    assert fields["include_patterns"]["superseded_by"] == "folder_picker"
    assert fields["include_patterns"]["default"] == []
    assert fields["include_patterns"]["type"] == "list"
    assert fields["include_patterns"]["required"] is False


def test_a_required_field_is_never_deferred():
    """A hidden required field would be an unsubmittable form.

    None of today's connectors has one, which is why this is a guard rather than a case.
    """
    for spec in CATALOG:
        for field in _config_fields(spec):
            assert not (field["required"] and field.get("superseded_by"))


def _spec(short_name: str):
    return next(entry for entry in CATALOG if entry.short_name == short_name)


# ---------------------------------------------------------------------------
# Browsing when there is nothing to browse
# ---------------------------------------------------------------------------


def _app(factory: sessionmaker[Session], tmp_path) -> TestClient:
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    return TestClient(create_app(factory, store))


def _source(factory: sessionmaker[Session], **values) -> str:
    with factory() as session:
        source = Source(**values)
        session.add(source)
        session.commit()
        return source.id


def test_browsing_a_connection_that_has_no_tree_is_refused_in_words(
    factory: sessionmaker[Session], tmp_path
) -> None:
    """The picker must never be opened on a mailbox — and must say why if it is."""
    client = _app(factory, tmp_path)
    source_id = _source(factory, kind="gmail", display_name="Mail", config={})
    with client:
        response = client.get(f"/api/sources/{source_id}/browse", headers=ADMIN_HEADERS)
        assert response.status_code == 422
        assert "as a whole" in response.json()["detail"]
        assert client.get("/api/sources/nope/browse", headers=ADMIN_HEADERS).status_code == 404


def test_browsing_before_authorization_says_so_rather_than_failing_obscurely(
    factory: sessionmaker[Session], tmp_path
) -> None:
    client = _app(factory, tmp_path)
    source_id = _source(
        factory,
        kind="sharepoint_online",
        display_name="SharePoint",
        status="pending_auth",
        config={},
    )
    with client:
        response = client.get(f"/api/sources/{source_id}/browse", headers=ADMIN_HEADERS)
        assert response.status_code == 409
        assert "authorization" in response.json()["detail"]


def test_a_browse_that_fails_reports_the_provider_and_changes_nothing(
    factory: sessionmaker[Session], tmp_path
) -> None:
    """A failed browse is an operator-facing condition, not a broken page.

    An unlicensed service, a grant narrowed after authorization and an outage are
    indistinguishable from here, so the answer names the connector and carries the
    provider's own words — and the connection's scope is left exactly as it was, which
    is what lets the UI keep saying "still syncing everything it can reach".
    """
    client = _app(factory, tmp_path)
    source_id = _source(
        factory, kind="sharepoint_online", display_name="SharePoint", config={}
    )  # authorized in the operator's eyes, but no credentials were ever stored
    with client:
        response = client.get(f"/api/sources/{source_id}/browse", headers=ADMIN_HEADERS)
        assert response.status_code == 502
        assert response.json()["detail"].startswith("SharePoint Online:")
        listed = {row["id"]: row for row in client.get("/api/sources", headers=ADMIN_HEADERS).json()}
        assert listed[source_id]["scope"]["scoped"] is False


def test_a_provider_that_returns_nothing_browses_as_an_empty_tree(tmp_path) -> None:
    """An empty drive is a real answer and is passed through as one.

    Inventing a placeholder folder here would be worse than useless: an operator would
    select something that does not exist, and the sync would quietly index nothing.
    """
    from tests.connector_replay import Recorded, build

    connector, _client = build(
        "onedrive",
        {
            "GET https://graph.microsoft.com/v1.0/me/drive": Recorded(
                {"id": "drive-1", "name": "OneDrive"}
            ),
            "GET https://graph.microsoft.com/v1.0/drives/drive-1/root/children": Recorded(
                {"value": []}
            ),
        },
        staging=tmp_path,
        config={"mirror_permissions": False},
    )
    try:
        assert connector.browse_children(None) == []
    finally:
        connector.close()


def test_an_empty_selection_is_a_choice_the_api_accepts(
    factory: sessionmaker[Session], tmp_path
) -> None:
    """"The whole source" has to be sayable, not only arrivable at by never choosing.

    The picker offers it as a button, so the endpoint has to answer an empty selection
    without treating it as a mistake — and, on a connection that was already unscoped,
    without reporting a change that would suspend the tombstone guard for a run.
    """
    client = _app(factory, tmp_path)
    source_id = _source(factory, kind="onedrive", display_name="Drive", config={})
    with client:
        confirmed = client.put(
            f"/api/sources/{source_id}/scope", json={"roots": []}, headers=ADMIN_HEADERS
        ).json()
        assert confirmed["scope"]["scoped"] is False and confirmed["changed"] is False
        assert confirmed["scope"]["decided"] is True

        narrowed = client.put(
            f"/api/sources/{source_id}/scope",
            json={"roots": [{"id": "f-mandate", "type": "folder", "title": "Mandate"}]},
            headers=ADMIN_HEADERS,
        ).json()
        assert narrowed["scope"]["root_count"] == 1 and narrowed["changed"] is True
        assert narrowed["existing_object_count"] == 0
        assert narrowed["would_remove_existing"] is False

        # Clearing it again is a widening: a real change, and the next scan has to be full.
        widened = client.put(
            f"/api/sources/{source_id}/scope", json={"roots": []}, headers=ADMIN_HEADERS
        ).json()
        assert widened["scope"]["scoped"] is False and widened["changed"] is True


def test_a_browse_node_survives_the_round_trip_into_a_selection():
    """The ids a connector needs to find a folder must not be lost on the way in.

    `browse` emits `node_metadata`; the selection previously read only `metadata`, so a
    faithfully round-tripped browse node arrived stripped of its drive/folder ids. The
    traversal then skipped every root and the sync reported success having indexed
    nothing — the worst possible failure for a permissions-bearing index.
    """
    from knowledge_index.connectors import scoping

    browse_node = {
        "source_node_id": "folder:b!drive-abc|01FOLDERID",
        "node_type": "folder",
        "title": "Mandate-2026-Litigation",
        "node_metadata": {"site_id": "host,g1,g2", "drive_id": "b!drive-abc", "folder_id": "01FOLDERID"},
    }
    roots = scoping.parse_roots({"roots": [browse_node]})
    assert roots[0]["metadata"]["drive_id"] == "b!drive-abc"
    assert roots[0]["metadata"]["folder_id"] == "01FOLDERID"

    selections = scoping.to_node_selections({"roots": [browse_node]})
    assert selections[0].node_metadata["folder_id"] == "01FOLDERID"


def test_a_selection_resolves_from_its_node_id_when_metadata_is_absent():
    """Metadata is an optimisation; the id alone must still locate the folder."""
    from knowledge_index.connectors.sources.sharepoint_online.source import (
        SharePointOnlineSource,
    )

    parse = SharePointOnlineSource._parse_browse_node_id
    assert parse("folder:b!drive-abc|01FOLDERID") == {"drive_id": "b!drive-abc", "node_id": "01FOLDERID"}
    assert parse("drive:host,g1,g2|b!drive-abc") == {"site_id": "host,g1,g2", "drive_id": "b!drive-abc"}
    assert parse("site:host,g1,g2") == {"site_id": "host,g1,g2"}
    assert parse("nonsense") == {}
