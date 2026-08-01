"""Subscription reconciliation, renewal, and race-safe event-to-sync wakeups."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.connectors.events.base import (
    ConnectorEventAdapter,
    load_adapter,
)
from knowledge_index.connectors.events.sharepoint import verify_client_state
from knowledge_index.connectors.events.transports import (
    IncomingNotification,
)
from knowledge_index.connectors.registry import BY_NAME
from knowledge_index.db.models import ConnectorEventSubscription, Source
from knowledge_index.sync.runs import enqueue_sync

SYNCABLE = {"active", "error"}


def _log(message: str) -> None:
    print(f"[ki events] {message}", file=sys.stderr, flush=True)


def adapters(
    config: AppConfig, session_factory: sessionmaker[Session]
) -> dict[str, ConnectorEventAdapter]:
    result: dict[str, ConnectorEventAdapter] = {}
    for spec in BY_NAME.values():
        if not spec.event_adapter:
            continue
        adapter = load_adapter(spec.event_adapter, config, session_factory)
        result[adapter.key] = adapter
    return result


def reconcile_once(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> dict:
    """Make local subscription rows and upstream provider state converge."""
    moment = now or datetime.now(UTC)
    by_key = adapters(config, session_factory)
    report = {"created": 0, "renewed": 0, "deleted": 0, "errors": []}
    with session_factory() as session:
        sources = session.scalars(
            select(Source).where(Source.status.in_(SYNCABLE)).order_by(Source.id)
        ).all()
        for source in sources:
            spec = BY_NAME.get(source.kind)
            if spec is None or not spec.event_adapter:
                continue
            adapter = next(
                (
                    item
                    for item in by_key.values()
                    if item.__class__.__module__ + ":" + item.__class__.__name__
                    == spec.event_adapter
                ),
                None,
            )
            if adapter is None:
                continue
            _reconcile_source(session, source, adapter, moment, report)
        session.commit()
    return report


def _reconcile_source(
    session: Session,
    source: Source,
    adapter: ConnectorEventAdapter,
    moment: datetime,
    report: dict,
) -> None:
    desired, coverage = adapter.desired(source)
    existing = {
        row.target: row
        for row in session.scalars(
            select(ConnectorEventSubscription).where(
                ConnectorEventSubscription.source_id == source.id,
                ConnectorEventSubscription.adapter == adapter.key,
            )
        )
    }
    wanted = {item.target: item for item in desired}

    # Do not tear down a healthy upstream subscription merely because a deployment secret
    # disappeared during a restart. Mark it honestly and let reconciliation resume when
    # the operator restores the transport.
    if not adapter.configured:
        for row in existing.values():
            row.status = "transport_unconfigured"
            row.last_error = coverage.detail
        return

    for target, row in list(existing.items()):
        if target in wanted:
            continue
        try:
            adapter.delete(source, row)
            session.delete(row)
            report["deleted"] += 1
        except Exception as exc:  # noqa: BLE001 - retry next reconciliation
            _record_error(row, exc, report)

    for target, item in wanted.items():
        row = existing.get(target)
        if row is None:
            row = ConnectorEventSubscription(
                source_id=source.id,
                adapter=adapter.key,
                transport=adapter.transport,
                target=target,
                status="pending",
                detail=dict(item.detail),
            )
            session.add(row)
            session.flush()
            try:
                state = adapter.create(source, item)
                _apply(row, state, moment)
                report["created"] += 1
                _log(f"created {adapter.key} subscription for {source.display_name}: {target}")
            except Exception as exc:  # noqa: BLE001 - persisted and retried next pass
                _record_error(row, exc, report)
            continue

        expiry = _aware(row.expires_at) if row.expires_at else None
        if row.external_id and expiry and expiry > moment + adapter.renew_before:
            if row.status != "active":
                row.status = "active"
                row.last_error = None
            continue
        try:
            if row.external_id:
                state = adapter.renew(source, row)
                report["renewed"] += 1
                _log(f"renewed {adapter.key} subscription for {source.display_name}: {target}")
            else:
                state = adapter.create(source, item)
                report["created"] += 1
            _apply(row, state, moment)
        except Exception as exc:  # noqa: BLE001 - persisted and retried next pass
            _record_error(row, exc, report)


def _transport_family(
    config: AppConfig, session_factory: sessionmaker[Session], adapter_key: str
) -> set[str]:
    """Every adapter key sharing the transport a notification arrived on.

    One broker stream serves a whole provider family — SharePoint and OneDrive both
    receive Microsoft Graph notifications through the same Event Hubs consumer.
    Resolving only the consumer's own adapter key would silently drop the other
    connector's events.
    """
    by_key = adapters(config, session_factory)
    adapter = by_key.get(adapter_key)
    if adapter is None:
        return {adapter_key}
    return {key for key, item in by_key.items() if item.transport == adapter.transport}


def handle_notifications(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    adapter_key: str,
    notifications: list[IncomingNotification],
) -> bool:
    """Resolve provider ids, enqueue one sync per source, then let transport acknowledge."""
    try:
        family = _transport_family(config_getter(), session_factory, adapter_key)
        source_ids: set[str] = set()
        with session_factory() as session:
            for notification in notifications:
                row = session.scalar(
                    select(ConnectorEventSubscription).where(
                        ConnectorEventSubscription.adapter.in_(sorted(family)),
                        ConnectorEventSubscription.external_id == notification.external_id,
                    )
                )
                # Stale lifecycle messages and subscriptions deleted locally are safe to
                # consume. Replaying them forever cannot make a source appear.
                if row is None:
                    continue
                # A subscription created with a clientState stores its digest; any
                # notification for it must present the matching value. Rows without a
                # digest (Google Workspace events carry none) skip the check.
                expected = (row.detail or {}).get("client_state_digest")
                if expected is not None and not verify_client_state(
                    expected, notification.client_state
                ):
                    _log(
                        f"rejected {row.adapter} event for subscription "
                        f"{notification.external_id}: clientState did not match"
                    )
                    continue
                source = session.get(Source, row.source_id)
                row.last_event_at = datetime.now(UTC)
                if source is not None and source.status in SYNCABLE:
                    source_ids.add(source.id)
            session.commit()

        if source_ids:
            outcome = enqueue_sync(
                session_factory,
                config_getter(),
                source_ids=source_ids,
                trigger="event",
            )
            for run in outcome.runs:
                _log(f"event queued sync for source {run.source_id} (run {run.run_id})")
            for skipped in outcome.skipped:
                # "Already in flight" is successful coalescing: that run will drain the
                # same provider delta cursor, so the broker message can be acknowledged.
                _log(f"event coalesced for source {skipped.source_id}: {skipped.reason}")
        return True
    except Exception as exc:  # noqa: BLE001 - false means Pub/Sub retries / no EH checkpoint
        _log(f"event was not acknowledged: {type(exc).__name__}: {exc}")
        return False


def event_delivery_payload(
    session: Session,
    source: Source,
    config: AppConfig,
) -> dict:
    """Reader-facing live-event status for a source row."""
    spec = BY_NAME.get(source.kind)
    if spec is None or not spec.event_adapter:
        return {
            "supported": False,
            "mode": "reconciliation_only",
            "status": "not_supported",
            "detail": "This connector has no provider event adapter; its policy interval drives sync.",
            "targets": 0,
            "active": 0,
        }
    local_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    adapter = load_adapter(spec.event_adapter, config, local_factory)
    desired, coverage = adapter.desired(source)
    rows = session.scalars(
        select(ConnectorEventSubscription).where(
            ConnectorEventSubscription.source_id == source.id,
            ConnectorEventSubscription.adapter == adapter.key,
        )
    ).all()
    active = [row for row in rows if row.status == "active"]
    errors = [row.last_error for row in rows if row.last_error]
    status = coverage.mode
    if coverage.mode == "live":
        status = (
            "active"
            if len(active) >= len(desired) and desired
            else "error"
            if errors
            else "pending"
        )
    return {
        "supported": True,
        "mode": coverage.mode,
        "status": status,
        "detail": errors[0] if errors else coverage.detail,
        "targets": len(desired),
        "active": len(active),
        "last_event_at": _latest(row.last_event_at for row in rows),
        "next_expiration_at": _earliest(row.expires_at for row in active),
        "transport": adapter.transport,
    }


def delete_upstream_for_source(
    session_factory: sessionmaker[Session], config: AppConfig, source_id: str
) -> list[str]:
    """Best-effort provider cleanup before a connection row is removed."""
    failures: list[str] = []
    by_key = adapters(config, session_factory)
    with session_factory() as session:
        source = session.get(Source, source_id)
        if source is None:
            return failures
        rows = session.scalars(
            select(ConnectorEventSubscription).where(
                ConnectorEventSubscription.source_id == source_id
            )
        ).all()
        for row in rows:
            adapter = by_key.get(row.adapter)
            if adapter is None or not adapter.configured:
                continue
            try:
                adapter.delete(source, row)
            except Exception as exc:  # noqa: BLE001 - source removal still proceeds
                failures.append(f"{row.target}: {type(exc).__name__}: {exc}")
    return failures


def run_event_manager_loop(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
    *,
    stop_event: threading.Event | None = None,
) -> None:
    stop = stop_event or threading.Event()
    _log("subscription manager started")
    while not stop.is_set():
        config = config_getter()
        try:
            report = reconcile_once(session_factory, config)
            if any(report[key] for key in ("created", "renewed", "deleted")):
                _log(
                    f"reconciled: {report['created']} created, {report['renewed']} renewed, "
                    f"{report['deleted']} deleted"
                )
        except Exception as exc:  # noqa: BLE001 - never silently lose renewals
            _log(f"subscription reconciliation failed: {type(exc).__name__}: {exc}")
        if stop.wait(config.connectors.events.reconcile_seconds):
            break
    _log("subscription manager stopped")


def start_background_event_manager(
    session_factory: sessionmaker[Session],
    config_getter: Callable[[], AppConfig],
) -> list[threading.Thread]:
    """Start renewal plus configured transports beside ``ki serve``."""
    config = config_getter()
    if not config.connectors.events.enabled:
        _log("provider events disabled; policy intervals remain active")
        return []
    if os.environ.get("KI_CONNECTOR_EVENTS", "1").strip() == "0":
        _log("provider events disabled by KI_CONNECTOR_EVENTS=0")
        return []
    handler = lambda key, messages: handle_notifications(  # noqa: E731
        session_factory, config_getter, key, messages
    )
    transport_threads: list[threading.Thread] = []
    consumed_transports: set[str] = set()
    for adapter in adapters(config, session_factory).values():
        # One consumer per broker stream. SharePoint and OneDrive share the Event Hubs
        # transport; starting it twice would put two competing readers in one consumer
        # group, and handle_notifications already resolves the whole transport family.
        if adapter.transport in consumed_transports:
            continue
        started = adapter.start_consumers(config_getter, handler)
        if started:
            consumed_transports.add(adapter.transport)
        transport_threads.extend(started)
    renewal = threading.Thread(
        target=run_event_manager_loop,
        args=(session_factory, config_getter),
        name="ki-connector-event-manager",
        daemon=True,
    )
    renewal.start()
    return [*transport_threads, renewal]


def _apply(row, state, moment: datetime) -> None:
    row.external_id = state.external_id
    row.expires_at = state.expires_at
    row.status = "active"
    row.last_renewed_at = moment
    row.last_error = None
    row.detail = {**(row.detail or {}), **state.detail}


def _record_error(row: ConnectorEventSubscription, exc: Exception, report: dict) -> None:
    message = f"{type(exc).__name__}: {str(exc)[:1000]}"
    row.status = "error"
    row.last_error = message
    report["errors"].append({"source_id": row.source_id, "target": row.target, "error": message})
    _log(f"{row.adapter} subscription failed for {row.target}: {message}")


def _latest(values) -> str | None:
    rows = [_aware(value) for value in values if value is not None]
    return max(rows).isoformat() if rows else None


def _earliest(values) -> str | None:
    rows = [_aware(value) for value in values if value is not None]
    return min(rows).isoformat() if rows else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
