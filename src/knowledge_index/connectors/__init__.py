"""Source connectors: SaaS and DMS estates, synced in-process.

Layout:

``runtime/``    auth, OAuth, HTTP, file staging, logging, error taxonomy
``sources/``    the connector implementations
``entities/``   the typed records each connector emits
``cursors/``    incremental-sync state schemas
``bridge.py``   adapts a connector onto ``SyncSource``
``registry.py`` the catalog and the factory the rest of the system talks to

The pipeline, the sync engine and the admin UI go through
:mod:`knowledge_index.connectors.registry`, so adding or replacing a connector never
reaches into core application code.
"""

from knowledge_index.connectors.bridge import ConnectorAdapter
from knowledge_index.connectors.registry import (
    CATALOG,
    ConnectorSpec,
    build_connector,
    catalog,
    get,
)

__all__ = [
    "CATALOG",
    "ConnectorAdapter",
    "ConnectorSpec",
    "build_connector",
    "catalog",
    "get",
]
