"""Provider events wake the existing delta sync without becoming a second sync engine."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.connectors import credentials as credential_store
from knowledge_index.connectors.events.auth import (
    application_client_secret,
    source_access_token,
)
from knowledge_index.connectors.events.base import (
    ConnectorEventAdapter,
    Coverage,
    DesiredSubscription,
    UpstreamSubscription,
)
from knowledge_index.connectors.events.google_drive import GoogleDriveEventAdapter
from knowledge_index.connectors.events.manager import (
    _reconcile_source,
    event_delivery_payload,
    handle_notifications,
    start_background_event_manager,
)
from knowledge_index.connectors.events.onedrive import OneDriveEventAdapter
from knowledge_index.connectors.events.sharepoint import SharePointEventAdapter
from knowledge_index.connectors.events.transports import (
    IncomingNotification,
    parse_google_message,
    parse_graph_payload,
)
from knowledge_index.db.models import ConnectorEventSubscription, Source


def _source(session: Session, *, kind: str, config: dict, cursor: str | None = None) -> Source:
    source = Source(
        kind=kind,
        display_name=kind,
        config=config,
        cursor=cursor,
        sync_policy={"mode": "continuous", "interval": "1h"},
    )
    session.add(source)
    session.flush()
    return source


def test_google_pubsub_cloud_event_maps_only_its_subscription() -> None:
    for source in (
        "//workspaceevents.googleapis.com/subscriptions/sub-1",
        # Drive Developer Preview currently publishes this form even though Google's
        # CloudEvent documentation shows the protocol-relative form above.
        "workspaceevents.googleapis.com/subscriptions/sub-1",
    ):
        parsed = parse_google_message(
            {
                "ce-source": source,
                "ce-type": "google.workspace.drive.permission.v3.edited",
            }
        )

        assert parsed == IncomingNotification(
            external_id="subscriptions/sub-1",
            event_type="google.workspace.drive.permission.v3.edited",
        )
    assert parse_google_message({"ce-source": "//drive.googleapis.com/files/file-1"}) is None


def test_event_renewal_reuses_a_current_source_access_token(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    monkeypatch.setenv(
        "KI_CONNECTOR_CREDENTIAL_KEY",
        base64.urlsafe_b64encode(b"e" * 32).decode(),
    )
    source = _source(
        session,
        kind="google_drive",
        config={"connector": {"scope_decided": True}},
    )
    credential_store.save(
        session,
        source.id,
        {
            "access_token": "current-access",
            "refresh_token": "refresh-1",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "expires_in": 3600,
        },
        provider="google_drive",
    )
    session.commit()

    async def unexpected_refresh(*_args, **_kwargs):
        raise AssertionError("a current token must not be refreshed")

    monkeypatch.setattr(
        "knowledge_index.connectors.events.auth.oauth_runtime.refresh_token",
        unexpected_refresh,
    )

    assert source_access_token(factory, source.id) == "current-access"


def test_event_renewal_persists_a_rotated_refresh_token(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    monkeypatch.setenv(
        "KI_CONNECTOR_CREDENTIAL_KEY",
        base64.urlsafe_b64encode(b"r" * 32).decode(),
    )
    source = _source(
        session,
        kind="sharepoint_online",
        config={"connector": {"scope_decided": True}},
    )
    credential_store.save(
        session,
        source.id,
        {
            "access_token": "expired-access",
            "refresh_token": "refresh-1",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "expires_in": 1,
        },
        provider="sharepoint_online",
    )
    session.commit()

    async def fake_refresh(_provider, **kwargs):
        assert kwargs["refresh_token"] == "refresh-1"
        return {
            "access_token": "fresh-access",
            "refresh_token": "refresh-2",
            "expires_in": 3600,
        }

    monkeypatch.setattr(
        "knowledge_index.connectors.events.auth.oauth_runtime.refresh_token",
        fake_refresh,
    )

    assert source_access_token(factory, source.id) == "fresh-access"
    with factory() as verify:
        stored = credential_store.load(verify, source.id)
    assert stored["access_token"] == "fresh-access"
    assert stored["refresh_token"] == "refresh-2"


def test_event_hubs_can_reuse_matching_encrypted_sharepoint_secret(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    monkeypatch.setenv(
        "KI_CONNECTOR_CREDENTIAL_KEY",
        base64.urlsafe_b64encode(b"h" * 32).decode(),
    )
    monkeypatch.delenv("KI_MICROSOFT_EVENTS_CLIENT_SECRET", raising=False)
    source = _source(
        session,
        kind="sharepoint_online",
        config={"connector": {"scope_decided": True}},
    )
    credential_store.save(
        session,
        source.id,
        {
            "client_id": "shared-app",
            "client_secret": "encrypted-source-secret",
            "refresh_token": "refresh",
        },
        provider="sharepoint_online",
    )
    session.commit()

    assert (
        application_client_secret(
            factory,
            client_id="shared-app",
            secret_env="KI_MICROSOFT_EVENTS_CLIENT_SECRET",
        )
        == "encrypted-source-secret"
    )
    assert (
        application_client_secret(
            factory,
            client_id="other-app",
            secret_env="KI_MICROSOFT_EVENTS_CLIENT_SECRET",
        )
        == ""
    )

    monkeypatch.setenv("KI_MICROSOFT_EVENTS_CLIENT_SECRET", "dedicated-secret")
    assert (
        application_client_secret(
            factory,
            client_id="other-app",
            secret_env="KI_MICROSOFT_EVENTS_CLIENT_SECRET",
        )
        == "dedicated-secret"
    )


def test_event_hubs_message_can_carry_several_graph_notifications() -> None:
    parsed = parse_graph_payload(
        {
            "value": [
                {
                    "subscriptionId": "sub-1",
                    "clientState": "state-1",
                    "changeType": "updated",
                },
                {
                    "subscriptionId": "sub-2",
                    "clientState": "state-2",
                    "changeType": "updated",
                },
            ]
        }
    )

    assert [item.external_id for item in parsed] == ["sub-1", "sub-2"]
    assert [item.client_state for item in parsed] == ["state-1", "state-2"]


def test_google_selected_folder_gets_live_descendant_coverage(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    monkeypatch.setenv("KI_GOOGLE_EVENTS_SERVICE_ACCOUNT_JSON", '{"type":"service_account"}')
    config = AppConfig()
    config.connectors.events.google_drive.topic = "projects/firm/topics/drive-events"
    config.connectors.events.google_drive.pull_subscription = (
        "projects/firm/subscriptions/ki"
    )
    source = _source(
        session,
        kind="google_drive",
        config={
            "connector": {
                "scope_decided": True,
                "roots": [
                    {
                        "id": "folder-1",
                        "type": "folder",
                        "title": "Matters",
                        "metadata": {
                            "folder_id": "folder-1",
                            "drive_id": "shared-1",
                        },
                    }
                ],
            }
        },
    )
    adapter = GoogleDriveEventAdapter(config, factory)

    desired, coverage = adapter.desired(source)

    assert [item.target for item in desired] == ["//drive.googleapis.com/files/folder-1"]
    assert desired[0].detail["include_descendants"] is True
    assert coverage.mode == "live"


def test_google_subscription_create_omits_zero_ttl(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    config = AppConfig()
    config.connectors.events.google_drive.topic = "projects/firm/topics/drive-events"
    source = _source(
        session,
        kind="google_drive",
        config={"connector": {"scope_decided": True}},
    )
    captured: dict = {}

    monkeypatch.setattr(
        "knowledge_index.connectors.events.google_drive.source_access_token",
        lambda *_args: "access",
    )

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(
            200,
            request=httpx.Request(method, url),
            json={
                "response": {
                    "name": "subscriptions/sub-1",
                    "expireTime": "2026-08-05T12:00:00Z",
                }
            },
        )

    monkeypatch.setattr(
        "knowledge_index.connectors.events.google_drive.httpx.request",
        fake_request,
    )

    state = GoogleDriveEventAdapter(config, factory).create(
        source,
        DesiredSubscription(
            "//drive.googleapis.com/files/folder-1",
            {"include_descendants": True},
        ),
    )

    assert state.external_id == "subscriptions/sub-1"
    assert "ttl" not in captured["json"]
    assert all(not event.endswith(".untrashed") for event in captured["json"]["eventTypes"])
    assert captured["json"]["driveOptions"] == {"includeDescendants": True}


def test_google_subscription_error_keeps_provider_message(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    config = AppConfig()
    config.connectors.events.google_drive.topic = "projects/firm/topics/drive-events"
    source = _source(
        session,
        kind="google_drive",
        config={"connector": {"scope_decided": True}},
    )
    monkeypatch.setattr(
        "knowledge_index.connectors.events.google_drive.source_access_token",
        lambda *_args: "access",
    )
    monkeypatch.setattr(
        "knowledge_index.connectors.events.google_drive.httpx.request",
        lambda method, url, **kwargs: httpx.Response(
            400,
            request=httpx.Request(method, url),
            json={
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "The subscription expiration time is in the past.",
                }
            },
        ),
    )

    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT.*expiration time"):
        GoogleDriveEventAdapter(config, factory).create(
            source,
            DesiredSubscription("//drive.googleapis.com/files/folder-1"),
        )


def test_google_whole_my_drive_truthfully_keeps_reconciliation_only(
    factory: sessionmaker[Session], session: Session
) -> None:
    source = _source(
        session,
        kind="google_drive",
        config={"connector": {"scope_decided": True, "roots": []}},
    )

    desired, coverage = GoogleDriveEventAdapter(AppConfig(), factory).desired(source)

    assert desired == []
    assert coverage.mode == "reconciliation_only"
    assert "whole My Drive root" in coverage.detail


def test_sharepoint_uses_every_discovered_drive_delta_cursor_as_an_event_target(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    monkeypatch.setenv("KI_MICROSOFT_EVENTS_CLIENT_SECRET", "event-consumer-secret")
    config = AppConfig()
    settings = config.connectors.events.microsoft_graph
    settings.notification_url = (
        "EventHub:https://events.servicebus.windows.net/eventhubname/graph"
        "?tenantId=firm.example"
    )
    settings.fully_qualified_namespace = "events.servicebus.windows.net"
    settings.event_hub_name = "graph"
    settings.tenant_id = "tenant"
    settings.client_id = "client"
    source = _source(
        session,
        kind="sharepoint_online",
        config={"connector": {"scope_decided": True, "roots": []}},
        cursor='{"drive_delta_tokens":{"drive-b":"token-b","drive-a":"token-a"}}',
    )

    desired, coverage = SharePointEventAdapter(config, factory).desired(source)

    assert [item.target for item in desired] == [
        "/drives/drive-a/root",
        "/drives/drive-b/root",
    ]
    assert coverage.mode == "live"


def _microsoft_events_config(monkeypatch) -> AppConfig:
    monkeypatch.setenv("KI_MICROSOFT_EVENTS_CLIENT_SECRET", "event-consumer-secret")
    config = AppConfig()
    settings = config.connectors.events.microsoft_graph
    settings.notification_url = (
        "EventHub:https://events.servicebus.windows.net/eventhubname/graph"
        "?tenantId=firm.example"
    )
    settings.fully_qualified_namespace = "events.servicebus.windows.net"
    settings.event_hub_name = "graph"
    settings.tenant_id = "tenant"
    settings.client_id = "client"
    return config


def test_onedrive_event_targets_come_from_the_selection_and_the_delta_cursor(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    config = _microsoft_events_config(monkeypatch)
    source = _source(
        session,
        kind="onedrive",
        config={
            "connector": {
                "scope_decided": True,
                "roots": [
                    {
                        "id": "f-mandate",
                        "type": "folder",
                        "title": "Mandate",
                        "metadata": {"drive_id": "drive-1", "folder_id": "f-mandate"},
                    }
                ],
            }
        },
        # The virtual app-folder drive has no delta feed and must never become a
        # subscription target.
        cursor=(
            '{"drive_delta_tokens":{"drive-2":"token-2"},'
            '"synced_drive_ids":{"appfolder":"OneDrive App Folder"}}'
        ),
    )

    desired, coverage = OneDriveEventAdapter(config, factory).desired(source)

    assert [item.target for item in desired] == [
        "/drives/drive-1/root",
        "/drives/drive-2/root",
    ]
    assert coverage.mode == "live"


def test_onedrive_before_its_first_sync_waits_rather_than_claiming_coverage(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    config = _microsoft_events_config(monkeypatch)
    source = _source(
        session,
        kind="onedrive",
        config={"connector": {"scope_decided": True, "roots": []}},
    )

    desired, coverage = OneDriveEventAdapter(config, factory).desired(source)

    assert desired == []
    assert coverage.mode == "waiting"
    assert "first OneDrive sync" in coverage.detail


def test_a_onedrive_notification_is_resolved_by_the_shared_graph_consumer(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    """SharePoint and OneDrive share one Event Hubs stream.

    The consumer thread announces itself with the SharePoint adapter key; a
    notification for a OneDrive subscription must still find its row instead of
    being dropped as unknown.
    """
    source = _source(
        session,
        kind="onedrive",
        config={"connector": {"scope_decided": True}},
    )
    client_state = "od-secret-state"
    row = ConnectorEventSubscription(
        source_id=source.id,
        adapter="microsoft_graph_onedrive",
        transport="azure_event_hubs",
        target="/drives/drive-1/root",
        external_id="upstream-od",
        status="active",
        detail={
            "client_state_digest": hashlib.sha256(client_state.encode()).hexdigest()
        },
    )
    session.add(row)
    session.commit()
    calls: list[dict] = []

    def fake_enqueue(_factory, _config, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(runs=[], skipped=[])

    monkeypatch.setattr(
        "knowledge_index.connectors.events.manager.enqueue_sync", fake_enqueue
    )

    handled = handle_notifications(
        factory,
        AppConfig,
        "microsoft_graph_sharepoint",
        [
            IncomingNotification("upstream-od", client_state, "updated"),
            # A wrong clientState on the same subscription stays rejected.
            IncomingNotification("upstream-od", "forged-state", "updated"),
        ],
    )

    assert handled is True
    assert calls == [{"source_ids": {source.id}, "trigger": "event"}]


def test_shared_transports_start_exactly_one_consumer(
    factory: sessionmaker[Session], monkeypatch
) -> None:
    """Two Microsoft adapters on one Event Hubs stream must not compete for it."""
    started: list[str] = []

    class _Consumer(ConnectorEventAdapter):
        renew_before = timedelta(hours=1)

        @property
        def configured(self) -> bool:
            return True

        def desired(self, source):
            return [], Coverage("waiting", "n/a")

        def create(self, source, desired):
            raise NotImplementedError

        def renew(self, source, current):
            raise NotImplementedError

        def delete(self, source, current):
            raise NotImplementedError

        def start_consumers(self, config_getter, handler):
            started.append(self.key)
            return [object()]

    class _GraphA(_Consumer):
        key = "graph_a"
        transport = "azure_event_hubs"

    class _GraphB(_Consumer):
        key = "graph_b"
        transport = "azure_event_hubs"

    class _Pubsub(_Consumer):
        key = "pubsub"
        transport = "google_pubsub"

    config = AppConfig()
    monkeypatch.setattr(
        "knowledge_index.connectors.events.manager.adapters",
        lambda _config, _factory: {
            "graph_a": _GraphA(config, factory),
            "graph_b": _GraphB(config, factory),
            "pubsub": _Pubsub(config, factory),
        },
    )
    monkeypatch.setattr(
        "knowledge_index.connectors.events.manager.run_event_manager_loop",
        lambda *_args, **_kwargs: None,
    )

    start_background_event_manager(factory, lambda: config)

    assert started == ["graph_a", "pubsub"]


class _FakeAdapter(ConnectorEventAdapter):
    key = "fake"
    transport = "fake_pull"
    renew_before = timedelta(hours=1)

    def __init__(self, config, factory):
        super().__init__(config, factory)
        self.created = 0
        self.renewed = 0
        self.deleted = 0

    @property
    def configured(self) -> bool:
        return True

    def desired(self, source):
        return [DesiredSubscription("target-1")], Coverage("live", "live", 1)

    def create(self, source, desired):
        self.created += 1
        return UpstreamSubscription(
            "upstream-1", datetime.now(UTC) + timedelta(days=2)
        )

    def renew(self, source, current):
        self.renewed += 1
        return UpstreamSubscription(
            current.external_id, datetime.now(UTC) + timedelta(days=2)
        )

    def delete(self, source, current):
        self.deleted += 1


def test_generic_reconciler_creates_then_renews_provider_subscription(
    factory: sessionmaker[Session], session: Session
) -> None:
    source = _source(
        session,
        kind="google_drive",
        config={"connector": {"scope_decided": True}},
    )
    adapter = _FakeAdapter(AppConfig(), factory)
    report = {"created": 0, "renewed": 0, "deleted": 0, "errors": []}
    now = datetime.now(UTC)

    _reconcile_source(session, source, adapter, now, report)
    session.flush()
    row = session.scalar(select(ConnectorEventSubscription))
    assert row.status == "active"
    assert row.external_id == "upstream-1"
    assert adapter.created == 1

    row.expires_at = now + timedelta(minutes=5)
    _reconcile_source(session, source, adapter, now, report)
    assert adapter.renewed == 1
    assert report == {"created": 1, "renewed": 1, "deleted": 0, "errors": []}


def test_graph_event_with_valid_client_state_coalesces_into_event_triggered_sync(
    factory: sessionmaker[Session], session: Session, monkeypatch
) -> None:
    source = _source(
        session,
        kind="sharepoint_online",
        config={"connector": {"scope_decided": True}},
    )
    client_state = "secret-state"
    row = ConnectorEventSubscription(
        source_id=source.id,
        adapter="microsoft_graph_sharepoint",
        transport="azure_event_hubs",
        target="/drives/drive-1/root",
        external_id="upstream-1",
        status="active",
        detail={
            "client_state_digest": hashlib.sha256(client_state.encode()).hexdigest()
        },
    )
    session.add(row)
    session.commit()
    calls: list[dict] = []

    def fake_enqueue(_factory, _config, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(runs=[], skipped=[])

    monkeypatch.setattr(
        "knowledge_index.connectors.events.manager.enqueue_sync", fake_enqueue
    )

    handled = handle_notifications(
        factory,
        AppConfig,
        "microsoft_graph_sharepoint",
        [IncomingNotification("upstream-1", client_state, "updated")],
    )

    assert handled is True
    assert calls == [{"source_ids": {source.id}, "trigger": "event"}]
    with factory() as verify:
        assert verify.get(ConnectorEventSubscription, row.id).last_event_at is not None


def test_source_payload_distinguishes_event_transport_from_policy(
    session: Session,
) -> None:
    source = _source(
        session,
        kind="google_drive",
        config={
            "connector": {
                "scope_decided": True,
                "roots": [
                    {
                        "id": "folder-1",
                        "type": "folder",
                        "metadata": {"folder_id": "folder-1"},
                    }
                ],
            }
        },
    )

    payload = event_delivery_payload(session, source, AppConfig())

    assert payload["supported"] is True
    assert payload["mode"] == "unconfigured"
    assert payload["status"] == "unconfigured"
    assert payload["targets"] == 1
