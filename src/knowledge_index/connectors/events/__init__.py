"""Provider events wake connector delta feeds; they never write indexed state directly."""

from knowledge_index.connectors.events.manager import (
    event_delivery_payload,
    reconcile_once,
    start_background_event_manager,
)

__all__ = [
    "event_delivery_payload",
    "reconcile_once",
    "start_background_event_manager",
]
