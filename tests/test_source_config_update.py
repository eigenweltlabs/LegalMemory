"""Editing a connection's connector settings after it was created.

Until ``PUT /api/sources/{id}/config`` existed, the settings a connector declares —
which member a Dropbox team token acts as, whether the team space is indexed, an
excluded path — were writable only at creation, so exercising them meant deleting a
working connection and authorizing it again. These tests pin the contract: only
schema-declared fields are editable, values are validated against the connector's own
config model, scope bookkeeping stored in the same dict is untouchable, and an emptied
value falls back to the connector default rather than persisting as "".
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore
from knowledge_index.db.models import Source
from knowledge_index.web.app import create_app

ADMIN_HEADERS = {"x-ki-principals": "user:local-admin,role:admin"}
USER_HEADERS = {"x-ki-principals": "user:somebody"}
CREDENTIAL_KEY = "a25vd2xlZGdlLWluZGV4LXRlc3Qta2V5LTMyYnl0ZXM="  # 32 bytes, base64url


@pytest.fixture
def app_client(factory: sessionmaker[Session], tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KI_CONNECTOR_CREDENTIAL_KEY", CREDENTIAL_KEY)
    store = ConfigStore(tmp_path / "config.json")
    store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    with TestClient(create_app(factory, store)) as client:
        yield client


@pytest.fixture
def dropbox_source(factory: sessionmaker[Session]) -> str:
    with factory() as session:
        source = Source(
            kind="dropbox",
            display_name="Dropbox",
            config={"connector": {"roots": ["/mandate"], "scope_decided": True}},
        )
        session.add(source)
        session.commit()
        return source.id


def test_schema_declared_settings_are_editable_after_creation(
    app_client, factory, dropbox_source
) -> None:
    response = app_client.put(
        f"/api/sources/{dropbox_source}/config",
        json={"act_as_email": "partnerin@kanzlei.de", "index_team_space": False},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["changed"] == ["act_as_email", "index_team_space"]
    assert body["config"]["act_as_email"] == "partnerin@kanzlei.de"
    assert body["config"]["index_team_space"] is False

    with factory() as session:
        stored = session.get(Source, dropbox_source).config["connector"]
    assert stored["act_as_email"] == "partnerin@kanzlei.de"
    assert stored["index_team_space"] is False
    # Scope bookkeeping in the same dict is owned by the scope endpoint and survives.
    assert stored["roots"] == ["/mandate"]
    assert stored["scope_decided"] is True


def test_the_saved_settings_are_visible_on_the_source(
    app_client, dropbox_source
) -> None:
    app_client.put(
        f"/api/sources/{dropbox_source}/config",
        json={"act_as_email": "partnerin@kanzlei.de"},
        headers=ADMIN_HEADERS,
    )

    listed = app_client.get("/api/sources", headers=ADMIN_HEADERS).json()
    settings = next(row for row in listed if row["id"] == dropbox_source)["connector_settings"]
    assert settings == {"act_as_email": "partnerin@kanzlei.de"}


def test_an_emptied_setting_returns_to_the_connector_default(
    app_client, factory, dropbox_source
) -> None:
    app_client.put(
        f"/api/sources/{dropbox_source}/config",
        json={"act_as_email": "partnerin@kanzlei.de"},
        headers=ADMIN_HEADERS,
    )
    response = app_client.put(
        f"/api/sources/{dropbox_source}/config",
        json={"act_as_email": ""},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    with factory() as session:
        stored = session.get(Source, dropbox_source).config["connector"]
    assert "act_as_email" not in stored


def test_a_setting_the_connector_does_not_declare_is_refused(
    app_client, dropbox_source
) -> None:
    response = app_client.put(
        f"/api/sources/{dropbox_source}/config",
        json={"roots": []},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422
    assert "roots" in response.json()["detail"]


def test_a_value_the_schema_rejects_is_refused(app_client, dropbox_source) -> None:
    response = app_client.put(
        f"/api/sources/{dropbox_source}/config",
        json={"index_team_space": "banana"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422


def test_editing_settings_requires_an_administrator(app_client, dropbox_source) -> None:
    response = app_client.put(
        f"/api/sources/{dropbox_source}/config",
        json={"index_team_space": False},
        headers=USER_HEADERS,
    )

    assert response.status_code == 403
