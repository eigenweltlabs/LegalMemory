"""Google Workspace Events adapter for selected Drive folders and shared drives."""

from __future__ import annotations

import time
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from knowledge_index.connectors import scoping
from knowledge_index.connectors.events.auth import source_access_token
from knowledge_index.connectors.events.base import (
    ConnectorEventAdapter,
    Coverage,
    DesiredSubscription,
    UpstreamSubscription,
)
from knowledge_index.db.models import ConnectorEventSubscription, Source

API = "https://workspaceevents.googleapis.com/v1"
DRIVE_EVENT_TYPES = [
    "google.workspace.drive.file.v3.created",
    "google.workspace.drive.file.v3.moved",
    "google.workspace.drive.file.v3.contentChanged",
    "google.workspace.drive.file.v3.deleted",
    "google.workspace.drive.file.v3.trashed",
    "google.workspace.drive.file.v3.renamed",
    "google.workspace.drive.permission.v3.created",
    "google.workspace.drive.permission.v3.edited",
    "google.workspace.drive.permission.v3.deleted",
]
# Google currently advertises ``file.v3.untrashed`` but rejects it with
# TARGET_RESOURCE_ACCESS_DENIED for otherwise valid Shared Drive folders. Restores are
# therefore caught by the reconciliation interval until the provider accepts that event.


class GoogleDriveEventAdapter(ConnectorEventAdapter):
    key = "google_workspace_drive"
    transport = "google_pubsub"
    # Renew at least a day before Google's seven-day maximum.
    renew_before = timedelta(hours=36)

    @property
    def configured(self) -> bool:
        return self.config.connectors.events.google_drive.configured

    def start_consumers(
        self,
        config_getter: Callable,
        handler: Callable,
    ) -> list[threading.Thread]:
        from knowledge_index.connectors.events.transports import (
            start_google_pubsub_consumer,
        )

        return start_google_pubsub_consumer(
            self.session_factory, config_getter, handler
        )

    def desired(self, source: Source) -> tuple[list[DesiredSubscription], Coverage]:
        connector = (source.config or {}).get("connector") or {}
        scope = scoping.describe(connector)
        if not scope["decided"]:
            return [], Coverage("waiting", "Choose the folders before live events start.")
        roots = scoping.parse_roots(connector)
        if not roots:
            # Workspace Events cannot subscribe to the My Drive root. A whole-source
            # connection can span both My Drive and several shared drives, so claiming
            # complete live coverage here would be false. The Changes cursor still makes
            # scheduled reconciliation incremental.
            return [], Coverage(
                "reconciliation_only",
                "Google does not permit an event subscription on the whole My Drive root; "
                "the Drive Changes feed runs on the reconciliation interval. Select a "
                "folder or shared drive for live events.",
            )

        desired: list[DesiredSubscription] = []
        seen: set[str] = set()
        for root in roots:
            metadata = root.get("metadata") or {}
            folder_id = str(metadata.get("folder_id") or root["id"]).strip()
            drive_id = str(metadata.get("drive_id") or "").strip()
            node_type = str(root.get("type") or "folder")
            if not folder_id:
                continue
            if node_type == "drive" or (drive_id and folder_id == drive_id):
                target = f"//drive.googleapis.com/drives/{drive_id or folder_id}"
                detail = {"include_descendants": True, "scope": "shared_drive"}
            else:
                target = f"//drive.googleapis.com/files/{folder_id}"
                detail = {
                    "include_descendants": node_type != "file",
                    "scope": node_type,
                }
            if target not in seen:
                desired.append(DesiredSubscription(target, detail))
                seen.add(target)
        if not desired:
            return [], Coverage(
                "waiting",
                "The selected Drive roots do not contain provider folder identifiers; "
                "choose them again.",
            )
        if not self.configured:
            return desired, Coverage(
                "unconfigured",
                "Drive supports live events for this scope, but Pub/Sub is not configured "
                "on this appliance.",
                len(desired),
            )
        return desired, Coverage(
            "live",
            "Google Workspace events wake the Drive Changes feed; the interval is a "
            "reconciliation safety net.",
            len(desired),
        )

    def create(self, source: Source, desired: DesiredSubscription) -> UpstreamSubscription:
        body = {
            "targetResource": desired.target,
            "eventTypes": DRIVE_EVENT_TYPES,
            "payloadOptions": {"includeResource": False},
            "driveOptions": {
                "includeDescendants": bool(desired.detail.get("include_descendants"))
            },
            "notificationEndpoint": {
                "pubsubTopic": self.config.connectors.events.google_drive.topic
            },
        }
        # Omit ttl when creating: Google applies the maximum lifetime by default.
        # ``0s`` requests that maximum on PATCH, but POST interprets it as an already
        # expired subscription.
        payload = self._request("POST", f"{API}/subscriptions", source, json=body)
        subscription = self._operation(payload, source)
        return self._state(subscription)

    def renew(
        self, source: Source, current: ConnectorEventSubscription
    ) -> UpstreamSubscription:
        name = quote(str(current.external_id or ""), safe="/")
        payload = self._request(
            "PATCH",
            f"{API}/{name}",
            source,
            params={"updateMask": "ttl"},
            json={"name": current.external_id, "ttl": "0s"},
        )
        return self._state(self._operation(payload, source))

    def delete(self, source: Source, current: ConnectorEventSubscription) -> None:
        if not current.external_id:
            return
        name = quote(current.external_id, safe="/")
        self._request("DELETE", f"{API}/{name}", source, allow_missing=True)

    def _request(
        self,
        method: str,
        url: str,
        source: Source,
        *,
        allow_missing: bool = False,
        **kwargs,
    ) -> dict:
        token = source_access_token(self.session_factory, source.id)
        response = httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            **kwargs,
        )
        if allow_missing and response.status_code == 404:
            return {}
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = _google_error_message(response)
            raise RuntimeError(
                f"Google Workspace Events {method} failed ({response.status_code}): {message}"
            ) from exc
        return response.json() if response.content else {}

    def _operation(self, payload: dict, source: Source) -> dict:
        if payload.get("response"):
            return payload["response"]
        name = str(payload.get("name") or "")
        if not name:
            return payload
        for _ in range(60):
            operation = self._request("GET", f"{API}/{quote(name, safe='/')}", source)
            if operation.get("done"):
                if operation.get("error"):
                    error = operation["error"]
                    raise RuntimeError(
                        f"Google Workspace event operation failed: "
                        f"{error.get('code')} {error.get('message')}"
                    )
                return operation.get("response") or {}
            time.sleep(0.5)
        raise TimeoutError(f"Google Workspace event operation {name} did not finish in 30s")

    @staticmethod
    def _state(payload: dict) -> UpstreamSubscription:
        name = str(payload.get("name") or "")
        expiration = _parse_time(payload.get("expireTime"))
        if not name or expiration is None:
            raise RuntimeError(
                "Google Workspace Events returned no subscription name or expiration"
            )
        return UpstreamSubscription(
            name,
            expiration,
            {
                "provider_state": payload.get("state"),
                "uid": payload.get("uid"),
                "etag": payload.get("etag"),
            },
        )


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _google_error_message(response: httpx.Response) -> str:
    """Keep Google's actionable error without persisting headers or OAuth tokens."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or response.reason_phrase or "request rejected")[:800]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        status = str(error.get("status") or "").strip()
        message = str(error.get("message") or "").strip()
        rendered = f"{status}: {message}" if status and message else status or message
        if rendered:
            return rendered[:800]
    return str(payload)[:800]
