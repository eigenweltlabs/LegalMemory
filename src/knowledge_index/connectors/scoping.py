"""Which parts of a source get synced.

Pointing a connector at an entire drive is rarely what anyone wants. A firm's OneDrive
estate is mostly templates, scans and personal files; converting and embedding all of it
costs real money per document and dilutes retrieval, because weak matches on irrelevant
documents still take up result slots. For a German firm there is a legal edge to it too:
data minimisation makes "we index the matter folders" a far easier conversation with a
DPO than "we index everything".

So a connection carries **subtree roots**, not a flat folder list. A root means "this
folder and everything below it, now and in future" — which matters because firms open new
matter folders continuously, and a flat selection would go stale the day they do.

An empty selection still means the whole source, so existing connections are unaffected;
the admin UI is where operators are pushed to choose.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from knowledge_index.connectors.runtime.types import NodeSelectionData

# Where the roots live on the source row.
CONFIG_KEY = "roots"
# An empty roots list means "the whole source", so it cannot also tell us whether an
# operator deliberately chose that or simply has not reached the picker yet. Keep the
# decision separately: schedulers must not crawl a newly authorized estate in the few
# seconds before its folder picker is saved.
DECIDED_KEY = "scope_decided"


def parse_roots(connector_config: dict | None) -> list[dict]:
    """Read and normalize the configured subtree roots."""
    raw = (connector_config or {}).get(CONFIG_KEY) or []
    roots: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            continue
        node_id = str(entry.get("id") or entry.get("source_node_id") or "").strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        roots.append(
            {
                "id": node_id,
                "type": str(entry.get("type") or entry.get("node_type") or "folder"),
                "title": entry.get("title") or entry.get("path") or node_id,
                # `browse` emits this as `node_metadata`; accept both spellings. A
                # connector's traversal locates a folder from this metadata (drive id,
                # folder id), so dropping it does not narrow the sync — it silently
                # syncs nothing at all.
                "metadata": entry.get("metadata") or entry.get("node_metadata") or {},
            }
        )
    return roots


def to_node_selections(connector_config: dict | None) -> list[NodeSelectionData]:
    """Turn configured roots into the selection a connector's traversal understands."""
    return [
        NodeSelectionData(
            source_node_id=root["id"],
            node_type=root["type"],
            node_title=root["title"],
            node_metadata=root["metadata"],
        )
        for root in parse_roots(connector_config)
    ]


def fingerprint(connector_config: dict | None) -> str:
    """A stable digest of the selection, used to detect a re-scope.

    Order-insensitive and metadata-insensitive: reordering the roots in the UI, or a
    provider returning a different display title, is not a re-scope and must not trigger
    a full rebuild. Only the set of root ids counts.
    """
    ids = sorted(root["id"] for root in parse_roots(connector_config))
    payload = json.dumps(ids, separators=(",", ":"))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def describe(connector_config: dict | None) -> dict[str, Any]:
    """Human-readable scope summary for the admin UI and sync reports."""
    roots = parse_roots(connector_config)
    return {
        "decided": bool((connector_config or {}).get(DECIDED_KEY)),
        "scoped": bool(roots),
        "root_count": len(roots),
        "roots": [{"id": root["id"], "title": root["title"], "type": root["type"]} for root in roots],
        "fingerprint": fingerprint(connector_config),
    }
