"""Microsoft Graph change-notification adapter for SharePoint document libraries."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from knowledge_index.connectors import scoping
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
from knowledge_index.db.models import ConnectorEventSubscription, Source

GRAPH = "https://graph.microsoft.com/v1.0"
LIFETIME = timedelta(days=29)


class SharePointEventAdapter(ConnectorEventAdapter):
    key = "microsoft_graph_sharepoint"
    transport = "azure_event_hubs"
    renew_before = timedelta(days=3)

    @property
    def configured(self) -> bool:
        settings = self.config.connectors.events.microsoft_graph
        return bool(
            settings.coordinates_configured
            and application_client_secret(
                self.session_factory,
                client_id=settings.client_id,
                secret_env=settings.client_secret_env,
            )
        )

    def start_consumers(
        self,
        config_getter: Callable,
        handler: Callable,
    ) -> list[threading.Thread]:
        from knowledge_index.connectors.events.transports import (
            start_microsoft_event_hubs_consumer,
        )

        return start_microsoft_event_hubs_consumer(
            self.session_factory, config_getter, handler
        )

    def desired(self, source: Source) -> tuple[list[DesiredSubscription], Coverage]:
        connector = (source.config or {}).get("connector") or {}
        scope = scoping.describe(connector)
        if not scope["decided"]:
            return [], Coverage("waiting", "Choose the SharePoint scope before events start.")

        drive_ids: set[str] = set()
        for root in scoping.parse_roots(connector):
            metadata = root.get("metadata") or {}
            drive_id = str(metadata.get("drive_id") or "").strip()
            if not drive_id:
                prefix, _, value = str(root["id"]).partition(":")
                parts = value.split("|")
                if prefix == "drive" and len(parts) == 2:
                    drive_id = parts[1]
                elif prefix in ("folder", "file") and parts:
                    drive_id = parts[0]
            if drive_id:
                drive_ids.add(drive_id)

        # A whole source or a selected site learns its document-library ids during the
        # first scan. Both the old and current cursor schemas carry the delta-token keys,
        # so this is forward/backward compatible.
        try:
            cursor = json.loads(source.cursor or "{}")
        except (TypeError, ValueError):
            cursor = {}
        drive_ids.update(str(key) for key in (cursor.get("drive_delta_tokens") or {}))
        drive_ids.update(str(key) for key in (cursor.get("synced_drive_ids") or {}))

        desired = [
            DesiredSubscription(
                f"/drives/{drive_id}/root",
                {"drive_id": drive_id, "change_type": "updated"},
            )
            for drive_id in sorted(drive_ids)
            if drive_id
        ]
        if not desired:
            return [], Coverage(
                "waiting",
                "Run the first SharePoint sync so its document libraries can be discovered.",
            )
        if not self.configured:
            return desired, Coverage(
                "unconfigured",
                "SharePoint supports live events for these libraries, but Azure Event "
                "Hubs is not configured on this appliance.",
                len(desired),
            )
        return desired, Coverage(
            "live",
            "Microsoft Graph notifications wake each library's delta feed; the interval "
            "is a reconciliation safety net.",
            len(desired),
        )

    def create(self, source: Source, desired: DesiredSubscription) -> UpstreamSubscription:
        client_state = secrets.token_urlsafe(32)
        expiration = datetime.now(UTC) + LIFETIME
        payload = self._request(
            "POST",
            f"{GRAPH}/subscriptions",
            source,
            json={
                "changeType": "updated",
                "notificationUrl": (
                    self.config.connectors.events.microsoft_graph.notification_url
                ),
                "resource": desired.target,
                "expirationDateTime": expiration.isoformat().replace("+00:00", "Z"),
                "clientState": client_state,
            },
        )
        state = self._state(payload)
        return UpstreamSubscription(
            state.external_id,
            state.expires_at,
            {
                **state.detail,
                # Store only a verifier. The random value itself has no use after the
                # create request and must not become a plaintext secret in the database.
                "client_state_digest": _digest(client_state),
            },
        )

    def renew(
        self, source: Source, current: ConnectorEventSubscription
    ) -> UpstreamSubscription:
        if not current.external_id:
            raise RuntimeError("Microsoft Graph event subscription has no provider id")
        expiration = datetime.now(UTC) + LIFETIME
        payload = self._request(
            "PATCH",
            f"{GRAPH}/subscriptions/{quote(current.external_id, safe='')}",
            source,
            json={"expirationDateTime": expiration.isoformat().replace("+00:00", "Z")},
        )
        state = self._state(payload)
        return UpstreamSubscription(
            state.external_id,
            state.expires_at,
            dict(current.detail or {}),
        )

    def delete(self, source: Source, current: ConnectorEventSubscription) -> None:
        if not current.external_id:
            return
        self._request(
            "DELETE",
            f"{GRAPH}/subscriptions/{quote(current.external_id, safe='')}",
            source,
            allow_missing=True,
        )

    def _request(
        self,
        method: str,
        url: str,
        source: Source,
        *,
        allow_missing: bool = False,
        **kwargs,
    ) -> dict:
        response = httpx.request(
            method,
            url,
            headers={
                "Authorization": (
                    f"Bearer {source_access_token(self.session_factory, source.id)}"
                )
            },
            timeout=30.0,
            **kwargs,
        )
        if allow_missing and response.status_code == 404:
            return {}
        response.raise_for_status()
        return response.json() if response.content else {}

    @staticmethod
    def _state(payload: dict) -> UpstreamSubscription:
        external_id = str(payload.get("id") or "")
        expiration = _parse_time(payload.get("expirationDateTime"))
        if not external_id or expiration is None:
            raise RuntimeError("Microsoft Graph returned no subscription id or expiration")
        return UpstreamSubscription(
            external_id,
            expiration,
            {"resource": payload.get("resource"), "change_type": payload.get("changeType")},
        )


def verify_client_state(expected_digest: str | None, presented: object) -> bool:
    if not expected_digest:
        return False
    return secrets.compare_digest(expected_digest, _digest(str(presented or "")))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
