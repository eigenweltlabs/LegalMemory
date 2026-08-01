"""An OAuth connection must not exist until the provider has authorized it.

The failure these cover is one an operator hits on their first afternoon: they open the
connect form for SharePoint, are sent to Microsoft, and close the tab — because they need
a tenant admin, because the client secret was wrong, because the phone rang. What used to
be left behind was a connection in the "Live sources" table that said "Awaiting
authorization", 0 objects, "Never" synced, and stayed that way forever, with the firm's
OAuth client secret in an orphaned credential row behind it.

Every assertion here is about what is *not* in the database.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.connectors import pending_auth
from knowledge_index.connectors.runtime import oauth as oauth_runtime
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.db.models import (
    PendingSourceAuthorization,
    Project,
    ProjectGrant,
    Source,
    SourceCredential,
)
from knowledge_index.web.app import create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}
CREDENTIAL_KEY = "a25vd2xlZGdlLWluZGV4LXRlc3Qta2V5LTMyYnl0ZXM="  # 32 bytes, base64url
CLIENT_SECRET = "s3cr3t-from-the-firms-own-entra-app"

CONNECT_BODY = {
    "display_name": "Matter workspaces",
    "kind": "sharepoint_online",
    "config": {},
    "client_id": "cid-0001",
    "client_secret": CLIENT_SECRET,
    "sync_policy": {"mode": "continuous", "interval": "1h"},
}


@pytest.fixture
def app_client(factory: sessionmaker[Session], tmp_path: Path, monkeypatch):
    """An admin API client on a truncated database, with credential encryption armed."""
    monkeypatch.setenv("KI_CONNECTOR_CREDENTIAL_KEY", CREDENTIAL_KEY)
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    with TestClient(create_app(factory, store)) as client:
        yield client


def _counts(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as session:
        return {
            "sources": session.scalar(select(func.count()).select_from(Source)) or 0,
            "credentials": session.scalar(select(func.count()).select_from(SourceCredential)) or 0,
            "pending": session.scalar(
                select(func.count()).select_from(PendingSourceAuthorization)
            )
            or 0,
        }


def _issued_tokens(**overrides):
    async def _exchange(provider, **kwargs):
        _exchange.calls.append(kwargs)
        return {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "Files.Read.All",
            **overrides,
        }

    _exchange.calls = []
    return _exchange


def _expire_pending(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        for row in session.scalars(select(PendingSourceAuthorization)):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


# ------------------------------------------------------------------ the abandoned flow


def test_connecting_an_oauth_source_creates_no_source(app_client, factory) -> None:
    """The connect form answers with a provider URL, not with a connection."""
    response = app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS)

    assert response.status_code == 202  # accepted a handshake, did not create anything
    body = response.json()
    assert body["pending_authorization"] is True
    assert body["authorization_url"].startswith("https://login.microsoftonline.com/")
    assert f"state={body['state']}" in body["authorization_url"]

    assert app_client.get("/api/sources", headers=ADMIN_HEADERS).json() == []
    assert _counts(factory) == {"sources": 0, "credentials": 0, "pending": 1}


def test_abandoned_handshake_leaves_nothing_behind(app_client, factory) -> None:
    """Walk away at the consent screen: no source, no credential, nothing in the list."""
    app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS)
    _expire_pending(factory)

    # The next connection attempt sweeps the lapsed one out on its way past.
    app_client.post(
        "/api/sources",
        json={**CONNECT_BODY, "display_name": "Second try"},
        headers=ADMIN_HEADERS,
    )

    assert app_client.get("/api/sources", headers=ADMIN_HEADERS).json() == []
    assert _counts(factory) == {"sources": 0, "credentials": 0, "pending": 1}


def test_expired_state_cannot_be_redeemed(app_client, factory, monkeypatch) -> None:
    """Expiry is enforced on lookup, so a stale state is dead even before the sweep."""
    state = app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS).json()["state"]
    _expire_pending(factory)
    exchange = _issued_tokens()
    monkeypatch.setattr(oauth_runtime, "exchange_code", exchange)

    response = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )

    assert response.status_code == 404
    assert exchange.calls == []  # the code was never even presented to the provider
    assert _counts(factory) == {"sources": 0, "credentials": 0, "pending": 0}


def test_unknown_state_is_refused(app_client, factory) -> None:
    app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS)
    response = app_client.get(
        "/api/connectors/oauth/callback?code=authcode&state=not-a-state", follow_redirects=False
    )
    assert response.status_code == 404
    assert _counts(factory)["sources"] == 0


def test_a_rejected_code_exchange_leaves_nothing_behind(app_client, factory, monkeypatch) -> None:
    """The provider says no. An authorization code is single-use, so the handshake is over."""
    state = app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS).json()["state"]

    async def _refuse(provider, **kwargs):
        raise SourceAuthError("AADSTS7000215: invalid client secret provided")

    monkeypatch.setattr(oauth_runtime, "exchange_code", _refuse)
    response = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )

    assert response.status_code == 400
    assert "invalid client secret" in response.json()["detail"]
    assert _counts(factory) == {"sources": 0, "credentials": 0, "pending": 0}


def test_a_handshake_cannot_be_replayed(app_client, factory, monkeypatch) -> None:
    """The pending record is gone after the callback, so the same state is spent."""
    state = app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS).json()["state"]
    monkeypatch.setattr(oauth_runtime, "exchange_code", _issued_tokens())

    first = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )
    second = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )

    assert first.status_code == 303
    assert second.status_code == 404
    assert _counts(factory)["sources"] == 1


# --------------------------------------------------------------- the completed flow


def test_successful_callback_creates_exactly_one_source_with_credentials(
    app_client, factory, monkeypatch
) -> None:
    state = app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS).json()["state"]
    monkeypatch.setattr(oauth_runtime, "exchange_code", _issued_tokens())

    response = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?connected=sharepoint_online#connectors"
    assert _counts(factory) == {"sources": 1, "credentials": 1, "pending": 0}

    listed = app_client.get("/api/sources", headers=ADMIN_HEADERS).json()
    assert len(listed) == 1
    assert listed[0]["display_name"] == "Matter workspaces"
    assert listed[0]["status"] == "active"
    assert listed[0]["sync_policy"] == {"mode": "continuous", "interval": "1h"}


def test_the_deferred_source_carries_the_grants_and_project_chosen_at_setup(
    app_client, factory, monkeypatch
) -> None:
    """The connect form's answers survive the redirect intact — grants included."""
    project = app_client.post(
        "/api/projects",
        json={"key": "ma-2026", "name": "M&A 2026"},
        headers=ADMIN_HEADERS,
    ).json()
    body = {
        **CONNECT_BODY,
        "project_id": project["id"],
        "default_acl": [
            {"principal": "group:ma-team", "principal_kind": "group", "access": "allow"}
        ],
        "acl_by_path": {"/Restricted": [{"principal": "user:partner", "access": "allow"}]},
        "config": {"site_ids": ["site-1"]},
    }
    state = app_client.post("/api/sources", json=body, headers=ADMIN_HEADERS).json()["state"]
    monkeypatch.setattr(oauth_runtime, "exchange_code", _issued_tokens())
    app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )

    with factory() as session:
        source = session.scalars(select(Source)).one()
    assert source.project_id == project["id"]
    assert source.config["default_acl"] == [
        {"principal": "group:ma-team", "principal_kind": "group", "access": "allow"}
    ]
    assert source.config["acl_by_path"] == {
        "/Restricted": [{"principal": "user:partner", "access": "allow"}]
    }
    assert source.config["connector"] == {"site_ids": ["site-1"]}


def test_a_project_deleted_mid_handshake_takes_the_handshake_with_it(
    app_client, factory, monkeypatch
) -> None:
    """Filing the documents under no project at all would quietly widen who can reach them."""
    project = app_client.post(
        "/api/projects", json={"key": "gone", "name": "Gone"}, headers=ADMIN_HEADERS
    ).json()
    state = app_client.post(
        "/api/sources", json={**CONNECT_BODY, "project_id": project["id"]}, headers=ADMIN_HEADERS
    ).json()["state"]
    with factory() as session:
        session.execute(
            sql_delete(ProjectGrant).where(ProjectGrant.project_id == project["id"])
        )
        session.delete(session.get(Project, project["id"]))
        session.commit()
    exchange = _issued_tokens()
    monkeypatch.setattr(oauth_runtime, "exchange_code", exchange)

    response = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )

    assert response.status_code in (404, 422)  # cascaded away, or refused — never a source
    assert exchange.calls == []  # the code is not spent on a connection that cannot be filed
    assert _counts(factory) == {"sources": 0, "credentials": 0, "pending": 0}


# ------------------------------------------------------------------------ at-rest secrets


def test_the_client_secret_is_never_stored_in_the_clear(app_client, factory) -> None:
    app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS)
    with factory() as session:
        row = session.scalars(select(PendingSourceAuthorization)).one()
    assert CLIENT_SECRET not in row.payload
    assert "cid-0001" not in row.payload
    # Nothing sensitive leaks into the columns that are queried in the clear.
    assert CLIENT_SECRET not in str(row.config)
    assert row.key_fingerprint


def test_the_pkce_verifier_survives_the_redirect(app_client, factory, monkeypatch) -> None:
    """A verifier minted before the Source exists must still reach the token exchange."""
    real = oauth_runtime.get_provider("sharepoint_online")
    monkeypatch.setattr(
        oauth_runtime,
        "get_provider",
        lambda name: dataclasses.replace(real, requires_pkce=True),
    )
    created = app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS).json()
    assert "code_challenge=" in created["authorization_url"]
    exchange = _issued_tokens()
    monkeypatch.setattr(oauth_runtime, "exchange_code", exchange)

    app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={created['state']}",
        follow_redirects=False,
    )

    assert exchange.calls[0]["code_verifier"]
    assert exchange.calls[0]["client_secret"] == CLIENT_SECRET


# ------------------------------------------------------------------- re-authorization


def _connect_successfully(app_client, monkeypatch) -> str:
    state = app_client.post("/api/sources", json=CONNECT_BODY, headers=ADMIN_HEADERS).json()["state"]
    monkeypatch.setattr(oauth_runtime, "exchange_code", _issued_tokens())
    app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={state}", follow_redirects=False
    )
    return app_client.get("/api/sources", headers=ADMIN_HEADERS).json()[0]["id"]


def test_reauthorizing_an_existing_source_does_not_duplicate_it(
    app_client, factory, monkeypatch
) -> None:
    source_id = _connect_successfully(app_client, monkeypatch)
    started = app_client.post(f"/api/connectors/{source_id}/authorize", headers=ADMIN_HEADERS)
    assert started.status_code == 200

    monkeypatch.setattr(oauth_runtime, "exchange_code", _issued_tokens(refresh_token="rt-2"))
    response = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode2&state={started.json()['state']}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _counts(factory) == {"sources": 1, "credentials": 1, "pending": 0}
    with factory() as session:
        from knowledge_index.connectors import credentials as credential_store

        stored = credential_store.load(session, source_id)
    assert stored["refresh_token"] == "rt-2"
    # The single-use handshake fields do not survive into the live credential.
    assert "oauth_state" not in stored and "oauth_code_verifier" not in stored


def test_abandoning_a_reauthorization_leaves_the_source_working(
    app_client, factory, monkeypatch
) -> None:
    """Clicking re-authorize and then closing the tab must not stop a healthy connection."""
    source_id = _connect_successfully(app_client, monkeypatch)
    app_client.post(f"/api/connectors/{source_id}/authorize", headers=ADMIN_HEADERS)

    listed = app_client.get("/api/sources", headers=ADMIN_HEADERS).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "active"  # not demoted to pending_auth on the way out


def test_an_expired_reauthorization_state_is_refused(app_client, factory, monkeypatch) -> None:
    source_id = _connect_successfully(app_client, monkeypatch)
    started = app_client.post(
        f"/api/connectors/{source_id}/authorize", headers=ADMIN_HEADERS
    ).json()
    with factory() as session:
        from knowledge_index.connectors import credentials as credential_store

        stored = credential_store.load(session, source_id)
        stored["oauth_state_expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        credential_store.save(session, source_id, stored, provider="sharepoint_online")
        session.commit()

    response = app_client.get(
        f"/api/connectors/oauth/callback?code=authcode&state={started['state']}",
        follow_redirects=False,
    )

    assert response.status_code == 410
    with factory() as session:
        from knowledge_index.connectors import credentials as credential_store

        stored = credential_store.load(session, source_id)
    assert "oauth_state" not in stored  # burnt, so it cannot be presented a second time
    assert _counts(factory)["sources"] == 1


# ------------------------------------------------------- connectors that need no handshake


def test_a_local_folder_is_created_immediately(app_client, factory, tmp_path) -> None:
    """Nothing about the deferred OAuth path touches a connector that is already complete."""
    root = tmp_path / "matters"
    root.mkdir()
    response = app_client.post(
        "/api/sources",
        json={"display_name": "Local matters", "kind": "local_fs", "root": str(root)},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["status"] == "active"
    assert len(app_client.get("/api/sources", headers=ADMIN_HEADERS).json()) == 1
    assert _counts(factory) == {"sources": 1, "credentials": 0, "pending": 0}


def test_a_plugin_drop_is_created_immediately(app_client, factory, tmp_path) -> None:
    root = tmp_path / "drop"
    root.mkdir()
    response = app_client.post(
        "/api/sources",
        json={"display_name": "FDE drop", "kind": "plugin_drop", "root": str(root)},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 201
    assert _counts(factory) == {"sources": 1, "credentials": 0, "pending": 0}


def test_an_oauth_connector_without_client_credentials_is_refused(app_client, factory) -> None:
    response = app_client.post(
        "/api/sources",
        json={"display_name": "No app", "kind": "sharepoint_online", "config": {}},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 422
    assert _counts(factory) == {"sources": 0, "credentials": 0, "pending": 0}


# --------------------------------------------------------------------------- the sweep


def test_the_sweep_removes_lapsed_handshakes_and_spares_live_ones(
    factory: sessionmaker[Session], monkeypatch
) -> None:
    monkeypatch.setenv("KI_CONNECTOR_CREDENTIAL_KEY", CREDENTIAL_KEY)
    with factory() as session:
        for name, state in (("live", "state-live"), ("lapsed", "state-lapsed")):
            pending_auth.create(
                session,
                state=state,
                kind="sharepoint_online",
                display_name=name,
                project_id=None,
                config={},
                sync_policy={},
                provider="native",
                provider_connection_id=None,
                secret_values={"client_id": "c", "client_secret": CLIENT_SECRET},
            )
        session.commit()

    with factory() as session:
        session.scalars(
            select(PendingSourceAuthorization).where(
                PendingSourceAuthorization.state == "state-lapsed"
            )
        ).one().expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    with factory() as session:
        # Enforced on lookup first: the lapsed record is unusable before anything sweeps.
        assert pending_auth.claim(session, "state-lapsed") is None
        assert pending_auth.claim(session, "state-live") is not None
        assert pending_auth.sweep(session) == 1
        session.commit()

    with factory() as session:
        remaining = session.scalars(select(PendingSourceAuthorization)).all()
    assert [row.state for row in remaining] == ["state-live"]
