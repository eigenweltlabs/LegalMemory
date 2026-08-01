"""Outbound pull consumers for provider event brokers.

Both transports hand the manager only a subscription id plus an optional verifier.
Provider payloads are intentionally discarded: their sole authority is "check your
delta feed now", never "write this object into the index".
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import ConnectorEventCheckpoint


@dataclass(frozen=True)
class IncomingNotification:
    external_id: str
    client_state: str | None = None
    event_type: str | None = None


NotificationHandler = Callable[[str, list[IncomingNotification]], bool]


def _log(message: str) -> None:
    print(f"[ki events] {message}", file=sys.stderr, flush=True)


def parse_google_message(attributes: dict[str, str]) -> IncomingNotification | None:
    """Normalize a Pub/Sub CloudEvent without accepting its resource data as truth."""
    source = str(attributes.get("ce-source") or "").strip().lstrip("/")
    prefix = "workspaceevents.googleapis.com/"
    if source.startswith(prefix):
        source = source[len(prefix) :]
    if not source.startswith("subscriptions/"):
        return None
    return IncomingNotification(
        external_id=source,
        event_type=str(attributes.get("ce-type") or "") or None,
    )


def parse_graph_payload(payload: bytes | str | dict) -> list[IncomingNotification]:
    """Normalize all notifications Microsoft Graph put in one Event Hubs message."""
    if isinstance(payload, bytes):
        value = json.loads(payload.decode("utf-8"))
    elif isinstance(payload, str):
        value = json.loads(payload)
    else:
        value = payload
    rows = value.get("value") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        return []
    notifications: list[IncomingNotification] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        external_id = str(row.get("subscriptionId") or "").strip()
        if not external_id:
            continue
        notifications.append(
            IncomingNotification(
                external_id=external_id,
                client_state=(
                    str(row["clientState"]) if row.get("clientState") is not None else None
                ),
                event_type=str(row.get("changeType") or "") or None,
            )
        )
    return notifications


def start_google_pubsub_consumer(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    handler: NotificationHandler,
) -> list[threading.Thread]:
    """Start Drive's outbound Pub/Sub pull consumer when deployment settings exist."""
    config = config_getter()
    if not config.connectors.events.google_drive.configured:
        return []
    return [
        start_consumer_thread(
            "ki-google-pubsub-events",
            _run_google_pubsub,
            session_factory,
            config_getter,
            handler,
        )
    ]


def start_microsoft_event_hubs_consumer(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    handler: NotificationHandler,
) -> list[threading.Thread]:
    """Start SharePoint's outbound Event Hubs consumer when configured."""
    from knowledge_index.connectors.events.auth import application_client_secret

    config = config_getter()
    settings = config.connectors.events.microsoft_graph
    if not settings.coordinates_configured or not application_client_secret(
        session_factory,
        client_id=settings.client_id,
        secret_env=settings.client_secret_env,
    ):
        return []
    return [
        start_consumer_thread(
            "ki-microsoft-event-hubs",
            _run_azure_event_hubs,
            session_factory,
            config_getter,
            handler,
        )
    ]


def start_consumer_thread(name: str, target, *args) -> threading.Thread:
    """Common daemon lifecycle for connector-supplied transport consumers."""
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


def _run_google_pubsub(
    _session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    handler: NotificationHandler,
) -> None:
    try:
        from google.api_core.exceptions import DeadlineExceeded
        from google.cloud import pubsub_v1
        from google.oauth2 import service_account

        settings = config_getter().connectors.events.google_drive
        raw_json = os.environ.get(settings.service_account_json_env, "").strip()
        if raw_json:
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(raw_json),
                scopes=["https://www.googleapis.com/auth/pubsub"],
            )
        else:
            credentials = service_account.Credentials.from_service_account_file(
                settings.service_account_file,
                scopes=["https://www.googleapis.com/auth/pubsub"],
            )
        subscriber = pubsub_v1.SubscriberClient(credentials=credentials)
        _log(f"Google Pub/Sub consumer started for {settings.pull_subscription}")
        while True:
            try:
                response = subscriber.pull(
                    request={
                        "subscription": settings.pull_subscription,
                        "max_messages": 100,
                    },
                    timeout=35.0,
                )
            except DeadlineExceeded:
                continue
            ack_ids: list[str] = []
            for received in response.received_messages:
                attributes = dict(received.message.attributes)
                notification = parse_google_message(attributes)
                if notification is None:
                    _log(
                        "ignored Google Pub/Sub message with CloudEvent "
                        f"source={attributes.get('ce-source')!r}, "
                        f"type={attributes.get('ce-type')!r}, "
                        f"attribute_keys={sorted(attributes)}"
                    )
                    ack_ids.append(received.ack_id)
                    continue
                if handler("google_workspace_drive", [notification]):
                    ack_ids.append(received.ack_id)
            if ack_ids:
                subscriber.acknowledge(
                    request={
                        "subscription": settings.pull_subscription,
                        "ack_ids": ack_ids,
                    }
                )
    except Exception as exc:  # noqa: BLE001 - the reconciliation scheduler remains alive
        _log(f"Google Pub/Sub consumer stopped: {type(exc).__name__}: {exc}")


def _run_azure_event_hubs(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    handler: NotificationHandler,
) -> None:
    try:
        from azure.eventhub import EventHubConsumerClient
        from azure.identity import ClientSecretCredential
        from knowledge_index.connectors.events.auth import application_client_secret

        settings = config_getter().connectors.events.microsoft_graph
        secret = application_client_secret(
            session_factory,
            client_id=settings.client_id,
            secret_env=settings.client_secret_env,
        )
        if not secret:
            raise RuntimeError(
                "Microsoft Event Hubs has no dedicated client secret and no active "
                "SharePoint connection uses its configured client id"
            )
        credential = ClientSecretCredential(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=secret,
        )
        client = EventHubConsumerClient(
            fully_qualified_namespace=settings.fully_qualified_namespace,
            eventhub_name=settings.event_hub_name,
            consumer_group=settings.consumer_group,
            credential=credential,
        )
        _log(
            f"Microsoft Event Hubs consumer started for "
            f"{settings.fully_qualified_namespace}/{settings.event_hub_name}"
        )

        def on_event(partition_context, event) -> None:
            if event is None:
                return
            try:
                notifications = parse_graph_payload(event.body_as_str(encoding="UTF-8"))
                handled = not notifications or handler(
                    "microsoft_graph_sharepoint", notifications
                )
                if handled:
                    _save_event_hub_position(
                        session_factory,
                        str(partition_context.partition_id),
                        str(event.offset),
                        int(event.sequence_number),
                    )
            except Exception as exc:  # noqa: BLE001 - no checkpoint means broker replay
                _log(
                    f"Microsoft Event Hubs message was not checkpointed: "
                    f"{type(exc).__name__}: {exc}"
                )

        with client:
            # A position map is safer than one global "@latest": an existing
            # partition resumes after its durable offset, while a new/empty
            # partition starts at the beginning of the retention window. That
            # prevents an appliance restart from skipping notifications that
            # arrived while it was offline.
            positions = _event_hub_positions(session_factory)
            starting_position = {
                str(partition): positions.get(str(partition), "-1")
                for partition in client.get_partition_ids()
            }
            client.receive(
                on_event=on_event,
                starting_position=starting_position,
                starting_position_inclusive=False,
                max_wait_time=30,
            )
    except Exception as exc:  # noqa: BLE001 - the reconciliation scheduler remains alive
        _log(f"Microsoft Event Hubs consumer stopped: {type(exc).__name__}: {exc}")


def _event_hub_positions(session_factory: sessionmaker[Session]) -> dict[str, str]:
    with session_factory() as session:
        rows = session.scalars(
            select(ConnectorEventCheckpoint).where(
                ConnectorEventCheckpoint.transport == "azure_event_hubs"
            )
        ).all()
        return {row.partition: row.position for row in rows}


def _save_event_hub_position(
    session_factory: sessionmaker[Session],
    partition: str,
    position: str,
    sequence_number: int,
) -> None:
    with session_factory() as session:
        row = session.scalar(
            select(ConnectorEventCheckpoint).where(
                ConnectorEventCheckpoint.transport == "azure_event_hubs",
                ConnectorEventCheckpoint.partition == partition,
            )
        )
        if row is None:
            row = ConnectorEventCheckpoint(
                transport="azure_event_hubs",
                partition=partition,
                position=position,
                sequence_number=sequence_number,
            )
            session.add(row)
        else:
            row.position = position
            row.sequence_number = sequence_number
        session.commit()
