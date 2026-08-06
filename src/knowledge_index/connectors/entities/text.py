"""Render an entity's indexable text, and decide whether it is a document at all.

Not every entity a connector yields is a document. A traversal emits the containers it
walks through — drives, sites, teams, channels, notebooks — so that the documents inside
them arrive with breadcrumb ancestry. Those containers must not become source objects, or
the corpus fills with folder stubs that match every query weakly and clutter every result
list.

Entities that *are* documents but carry no file (a Teams message, a Confluence page body,
a list item) hold their content in their own typed fields, marked ``embeddable`` on the
schema. This module assembles those fields into the text the pipeline converts. Without
it such entities look empty and are dropped silently — which is exactly how a Teams sync
can report success and index nothing.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# Checked before container words, so a "DriveItem" or "ChatMessage" resolves as content
# even though it also names its container.
CONTENT_TOKENS = (
    "file",
    "document",
    "page",
    "article",
    "message",
    "email",
    "mail",
    "item",
    "attachment",
    "note",
    "event",
    "comment",
    "post",
    "block",
    "record",
    "task",
    "issue",
)

CONTAINER_TOKENS = (
    "account",
    "drive",
    "site",
    "matter",
    "team",
    "channel",
    "chat",
    "notebook",
    "section",
    "space",
    "folder",
    "directory",
    "calendar",
    "mailbox",
    "user",
    "group",
    "workspace",
    "database",
    "list",
    "library",
    "repository",
    "board",
    "project",
)

# Fields that describe plumbing rather than content. Rendering these would bury the
# actual text under identifiers and URLs, and embed noise into every vector.
SKIP_FIELDS = frozenset(
    {
        "entity_id",
        "breadcrumbs",
        "system_metadata",
        "access",
        "local_path",
        "url",
        "download_url",
        "web_url",
        "web_url_override",
        "size",
        "file_type",
        "mime_type",
        "etag",
        "ctag",
        "e_tag",
        "c_tag",
        "textual_representation",
        "deletion_status",
        "parent_reference",
        "quota",
    }
)

MAX_VALUE_CHARS = 20_000


def is_container(entity: Any) -> bool:
    """Whether this entity is a container walked through, not a document to index."""
    if getattr(entity, "local_path", None):
        return False
    name = type(entity).__name__.casefold()
    if any(token in name for token in CONTENT_TOKENS):
        return False
    return any(token in name for token in CONTAINER_TOKENS)


def _format(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()[:MAX_VALUE_CHARS]
    if isinstance(value, (list, tuple, set)):
        rendered = [_format(item) for item in value]
        return ", ".join(item for item in rendered if item)[:MAX_VALUE_CHARS]
    if isinstance(value, dict):
        pairs = [f"{key}: {_format(item)}" for key, item in value.items() if _format(item)]
        return "; ".join(pairs)[:MAX_VALUE_CHARS]
    return str(value)[:MAX_VALUE_CHARS]


def _label(field_name: str) -> str:
    return field_name.replace("_", " ").strip().capitalize()


def render_text(entity: Any) -> str:
    """Assemble the entity's indexable text from its ``embeddable`` fields.

    Falls back to any long string field when a schema marks nothing embeddable, so a
    connector that forgets the marker still produces something searchable rather than
    silently contributing an empty document.
    """
    existing = str(getattr(entity, "textual_representation", "") or "").strip()
    if existing:
        return existing

    fields = type(entity).model_fields
    lines: list[str] = []

    ancestry = [
        str(crumb.name).strip()
        for crumb in (getattr(entity, "breadcrumbs", None) or [])
        if getattr(crumb, "name", None) and str(crumb.name).strip()
    ]
    if ancestry:
        # Location matters for legal retrieval: which matter folder a message sits in is
        # often the strongest signal about what it concerns.
        lines.append(f"Location: {' → '.join(ancestry)}")

    embeddable: list[str] = []
    for field_name, info in fields.items():
        if field_name in SKIP_FIELDS:
            continue
        extra = info.json_schema_extra
        if isinstance(extra, dict) and extra.get("embeddable"):
            embeddable.append(field_name)

    if not embeddable:
        embeddable = [
            field_name
            for field_name in fields
            if field_name not in SKIP_FIELDS
            and isinstance(getattr(entity, field_name, None), str)
            and len(str(getattr(entity, field_name))) > 40
        ]

    for field_name in embeddable:
        rendered = _format(getattr(entity, field_name, None))
        if not rendered:
            continue
        # A substantial body reads better without a field label in front of it.
        if len(rendered) > 200 or "\n" in rendered:
            lines.append(rendered)
        else:
            lines.append(f"{_label(field_name)}: {rendered}")

    return "\n".join(lines).strip()
