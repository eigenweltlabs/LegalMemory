"""Read the semantic markers an entity schema puts on its fields.

Entity schemas mark which field is the stable id, the display name and the timestamps
instead of forcing every connector onto fixed attribute names. Everything that needs to
interpret an entity generically — the sync bridge, content staging — resolves those
fields through here, so the convention lives in exactly one place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from knowledge_index.connectors.runtime.types import FieldFlag


def flagged_value(entity: Any, flag: FieldFlag) -> Any:
    """Return the value of the field marked with ``flag``, or ``None``."""
    key = flag.value
    for field_name, field_info in type(entity).model_fields.items():
        extra = field_info.json_schema_extra
        if isinstance(extra, dict) and extra.get(key):
            return getattr(entity, field_name, None)
    return None


def entity_external_id(entity: Any) -> str | None:
    """The source system's stable id for this entity."""
    value = getattr(entity, "entity_id", None) or flagged_value(entity, FieldFlag.IS_ENTITY_ID)
    return str(value) if value else None


def entity_name(entity: Any) -> str:
    value = getattr(entity, "name", None) or flagged_value(entity, FieldFlag.IS_NAME)
    return str(value) if value else ""


def entity_mtime(entity: Any) -> datetime | None:
    value = getattr(entity, "updated_at", None) or flagged_value(entity, FieldFlag.IS_UPDATED_AT)
    if value is None:
        value = getattr(entity, "created_at", None) or flagged_value(
            entity, FieldFlag.IS_CREATED_AT
        )
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def entity_version_token(entity: Any) -> str | None:
    """A server-side change token: etag/ctag if the source provides one.

    Used both as the engine's cheap change hint and as the staging cache key, so a
    document whose token is unchanged is never downloaded twice.
    """
    for attribute in ("etag", "e_tag", "ctag", "c_tag", "version", "version_id"):
        value = getattr(entity, attribute, None)
        if value:
            return str(value)
    return None


def is_deletion(entity: Any) -> bool:
    """Whether the entity reports that the object was removed at source.

    Delta feeds signal removals as entities rather than as absences, so this is the
    only way an incremental sync learns to tombstone.
    """
    status = getattr(entity, "deletion_status", None)
    return str(status).casefold() == "removed" if status is not None else False
