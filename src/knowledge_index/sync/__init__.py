"""Read-only connector contract and durable synchronization engine."""

from knowledge_index.sync.base import (
    ChangeBatch,
    SourceCapabilities,
    SourceObjectObservation,
    SyncSource,
    UnsupportedOperation,
)
from knowledge_index.sync.engine import SyncEngine, SyncResult
from knowledge_index.sync.local import LocalFilesystemSource
from knowledge_index.sync.plugin import PluginDropSource

__all__ = [
    "ChangeBatch",
    "LocalFilesystemSource",
    "PluginDropSource",
    "SourceCapabilities",
    "SourceObjectObservation",
    "SyncEngine",
    "SyncResult",
    "SyncSource",
    "UnsupportedOperation",
]
