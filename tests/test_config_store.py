"""ConfigStore must observe out-of-process edits so the worker sees admin changes.

It must also stop the saved file from shadowing the deployment. Writing
``/data/config.json`` once used to switch every ``KI_*`` variable off for good: an
operator edited docker-compose, restarted, nothing changed, and nothing said why. With
security settings now in that file (auth mode, the MCP trusted-header escape hatch, the
OIDC endpoints) that is not a confusing default, it is a way to pin a deployment to an
insecure setting from a file written months earlier.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore, EnvironmentPinnedSetting
from tests.conftest import TEST_EMBEDDING_MODEL


def test_config_store_reloads_when_file_changes_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    app_store = ConfigStore(path)
    app_store.save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    assert app_store.get().retrieval.embedding_model == TEST_EMBEDDING_MODEL

    # A different process (e.g. the admin API) rewrites the shared config file.
    worker_store = ConfigStore(path)
    assert worker_store.get().retrieval.embedding_model == TEST_EMBEDDING_MODEL
    changed = app_store.get().model_copy(deep=True)
    changed.retrieval.embedding_model = "bge-m3"
    changed.retrieval.embedding_dimensions = 1024
    app_store.save(changed)

    # The worker's store must observe the new model on its next resolve, not serve a
    # permanently cached snapshot.
    reloaded = worker_store.get()
    assert reloaded.retrieval.embedding_model == "bge-m3"
    assert reloaded.retrieval.embedding_dimensions == 1024


def test_derived_index_name_binds_model_and_dimension(tmp_path: Path) -> None:
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    config.retrieval.embedding_model = "text-embedding-3-large"
    config.retrieval.embedding_dimensions = 3072
    assert config.embedding_signature() == "text-embedding-3-large-3072"
    assert config.derived_index_name() == "knowledge-index-chunks-text-embedding-3-large-3072"


def test_a_saved_file_no_longer_shadows_the_environment(tmp_path: Path, monkeypatch) -> None:
    """The exact sequence that produced the defect: save first, add the variable later."""
    path = tmp_path / "config.json"
    ConfigStore(path).save(AppConfig(artifact_dir=tmp_path / "artifacts"))
    assert ConfigStore(path).get().security.auth_mode == "trusted_header"

    # The operator edits docker-compose and restarts the appliance.
    monkeypatch.setenv("KI_SECURITY__AUTH_MODE", "oidc")
    monkeypatch.setenv("KI_SECURITY__MCP_ALLOW_TRUSTED_HEADER", "false")
    restarted = ConfigStore(path).get()
    assert restarted.security.auth_mode == "oidc"
    assert restarted.security.mcp_allow_trusted_header is False


def test_the_environment_only_wins_where_it_actually_speaks(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    saved = AppConfig(artifact_dir=tmp_path / "artifacts")
    saved.security.admin_groups = ["partners"]
    saved.retrieval.embedding_model = "bge-m3"
    ConfigStore(path).save(saved)

    monkeypatch.setenv("KI_SECURITY__AUTH_MODE", "oidc")
    config = ConfigStore(path).get()
    assert config.security.auth_mode == "oidc"  # from the environment
    assert config.security.admin_groups == ["partners"]  # still from the file
    assert config.retrieval.embedding_model == "bge-m3"


def test_saving_cannot_quietly_change_a_pinned_setting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KI_SECURITY__AUTH_MODE", "oidc")
    store = ConfigStore(tmp_path / "config.json")
    draft = store.get().model_copy(deep=True)
    draft.security.auth_mode = "trusted_header"

    with pytest.raises(EnvironmentPinnedSetting) as raised:
        store.save(draft)

    assert raised.value.paths == ["security.auth_mode"]
    assert "KI_SECURITY__AUTH_MODE" in str(raised.value)
    assert store.get().security.auth_mode == "oidc"


def test_runtime_configuration_still_works_around_a_pinned_setting(
    tmp_path: Path, monkeypatch
) -> None:
    """The admin UI reads the effective config, edits one field and puts the whole
    object back. Everything the environment does not pin must still save — and a
    separate process (the worker) must see it."""
    monkeypatch.setenv("KI_SECURITY__AUTH_MODE", "oidc")
    path = tmp_path / "config.json"
    app_store = ConfigStore(path)
    draft = app_store.get().model_copy(deep=True)
    draft.security.admin_groups = ["partners"]
    draft.retrieval.embedding_model = "bge-m3"
    app_store.save(draft)

    worker = ConfigStore(path).get()
    assert worker.security.admin_groups == ["partners"]
    assert worker.retrieval.embedding_model == "bge-m3"
    assert worker.security.auth_mode == "oidc"


def test_a_pinned_setting_is_never_written_into_the_file(tmp_path: Path, monkeypatch) -> None:
    """So it cannot outlive the deployment that set it."""
    monkeypatch.setenv("KI_SECURITY__AUTH_MODE", "oidc")
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(store.get().model_copy(deep=True))

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "auth_mode" not in on_disk["security"]
    assert on_disk["security"]["admin_groups"] == ["knowledge-index-admins"]

    # The variable goes away again: the value falls back to the code default rather
    # than to a copy of the environment the file quietly kept.
    monkeypatch.delenv("KI_SECURITY__AUTH_MODE")
    assert ConfigStore(path).get().security.auth_mode == "trusted_header"


def test_the_override_is_visible_and_logged(tmp_path: Path, monkeypatch, caplog) -> None:
    path = tmp_path / "config.json"
    stale = AppConfig(artifact_dir=tmp_path / "artifacts")
    stale.security.auth_mode = "trusted_header"
    ConfigStore(path).save(stale)

    monkeypatch.setenv("KI_SECURITY__AUTH_MODE", "oidc")
    with caplog.at_level(logging.WARNING, logger="knowledge_index.config_store"):
        precedence = ConfigStore(path).precedence()

    assert "security.auth_mode <- KI_SECURITY__AUTH_MODE" in caplog.text
    entry = next(item for item in precedence["environment"] if item["path"] == "security.auth_mode")
    assert entry == {
        "path": "security.auth_mode",
        "env_var": "KI_SECURITY__AUTH_MODE",
        "value": "oidc",
        "shadows_file": True,
        "file_value": "trusted_header",
    }
    assert precedence["rule"] == "environment > saved file > defaults"


def test_the_precedence_listing_reports_effective_values(tmp_path: Path, monkeypatch) -> None:
    """Every variable is a string; comparing "false" against false helps nobody."""
    monkeypatch.setenv("KI_SECURITY__MCP_ALLOW_TRUSTED_HEADER", "false")
    listing = ConfigStore(tmp_path / "config.json").precedence()["environment"]
    entry = next(
        item for item in listing if item["path"] == "security.mcp_allow_trusted_header"
    )
    assert entry["value"] is False


def test_the_precedence_listing_does_not_echo_a_shared_secret(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("KI_SECURITY__TRUSTED_HEADER_SECRET", "hunter2")
    listing = ConfigStore(tmp_path / "config.json").precedence()["environment"]
    entry = next(item for item in listing if item["path"] == "security.trusted_header_secret")
    assert entry["value"] == "***"


def test_the_admin_api_refuses_a_pinned_change_and_publishes_the_precedence(
    factory, tmp_path: Path, monkeypatch
) -> None:
    """A 409 an administrator can read beats a 200 that does nothing."""
    from fastapi.testclient import TestClient

    from knowledge_index.web.app import create_app

    # The MCP dev escape hatch: with it on, anything that reaches the port names itself
    # in a header and becomes any lawyer in the firm. A deployment that switched it off
    # must not be talked back into it by the config file.
    monkeypatch.setenv("KI_SECURITY__MCP_ALLOW_TRUSTED_HEADER", "false")
    admin = {"x-ki-principals": "user:local-admin,role:admin"}
    store = ConfigStore(tmp_path / "config.json")
    with TestClient(create_app(factory, store)) as client:
        effective = client.get("/api/config", headers=admin).json()
        assert effective["security"]["mcp_allow_trusted_header"] is False

        effective["security"]["mcp_allow_trusted_header"] = True
        refused = client.put("/api/config", json=effective, headers=admin)
        assert refused.status_code == 409
        assert "KI_SECURITY__MCP_ALLOW_TRUSTED_HEADER" in refused.json()["detail"]
        assert store.get().security.mcp_allow_trusted_header is False

        effective["security"]["mcp_allow_trusted_header"] = False
        effective["retrieval"]["rerank_enabled"] = True
        accepted = client.put("/api/config", json=effective, headers=admin)
        assert accepted.status_code == 200
        assert store.get().retrieval.rerank_enabled is True
        assert "security.mcp_allow_trusted_header" in {
            item["path"] for item in accepted.json()["environment"]
        }

        precedence = client.get("/api/config/precedence", headers=admin).json()
        assert precedence["rule"] == "environment > saved file > defaults"
        assert "security.mcp_allow_trusted_header" in {
            item["path"] for item in precedence["environment"]
        }
