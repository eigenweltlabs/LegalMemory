"""Microsoft Graph change-notification adapter for OneDrive drives.

OneDrive and SharePoint document libraries share one change model: a Graph
subscription on ``/drives/{id}/root`` delivered through Azure Event Hubs. The
subscription lifecycle (create, renew, delete, clientState verification) is inherited
from the SharePoint adapter; what differs is how the drive ids are discovered — a
OneDrive connection has no sites to enumerate, its drive id is learned from the folder
selection or from the first sync's delta cursor.

Both adapters share the same Event Hubs stream. The manager starts one consumer per
transport and resolves notifications across every adapter on that transport, so a
notification for a OneDrive subscription is never mistaken for — or dropped by —
the SharePoint side.
"""

from __future__ import annotations

import json

from knowledge_index.connectors import scoping
from knowledge_index.connectors.events.base import Coverage, DesiredSubscription
from knowledge_index.connectors.events.sharepoint import SharePointEventAdapter
from knowledge_index.db.models import Source


class OneDriveEventAdapter(SharePointEventAdapter):
    key = "microsoft_graph_onedrive"

    def desired(self, source: Source) -> tuple[list[DesiredSubscription], Coverage]:
        connector = (source.config or {}).get("connector") or {}
        scope = scoping.describe(connector)
        if not scope["decided"]:
            return [], Coverage("waiting", "Choose the OneDrive scope before events start.")

        drive_ids: set[str] = set()
        for root in scoping.parse_roots(connector):
            metadata = root.get("metadata") or {}
            drive_id = str(metadata.get("drive_id") or "").strip()
            if drive_id:
                drive_ids.add(drive_id)

        # An unscoped connection learns its drive id during the first crawl; both the
        # delta-token keys and the discovered-drive map carry it.
        try:
            cursor = json.loads(source.cursor or "{}")
        except (TypeError, ValueError):
            cursor = {}
        drive_ids.update(str(key) for key in (cursor.get("drive_delta_tokens") or {}))
        drive_ids.update(str(key) for key in (cursor.get("synced_drive_ids") or {}))
        # The app folder is a virtual drive with no delta feed and no subscription target.
        drive_ids.discard("appfolder")

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
                "Run the first OneDrive sync so its drive can be discovered.",
            )
        if not self.configured:
            return desired, Coverage(
                "unconfigured",
                "OneDrive supports live events for this drive, but Azure Event "
                "Hubs is not configured on this appliance.",
                len(desired),
            )
        return desired, Coverage(
            "live",
            "Microsoft Graph notifications wake the drive's delta feed; the interval "
            "is a reconciliation safety net.",
            len(desired),
        )
