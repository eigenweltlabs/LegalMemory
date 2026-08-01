"""Clio entity schemas.

Two content-bearing shapes: the matter (a container, yielded for breadcrumb context)
and the document (a file). Clio's API identifies both by integer id; entities carry
them as strings because every external id in this system is a string.

Reference: https://docs.developers.clio.com/api-reference/ (Matters, Documents)
"""

from datetime import datetime
from typing import Any, Dict, Optional

from knowledge_index.connectors.entities._base import (
    BaseEntity,
    Breadcrumb,
    DeletionEntity,
    FileEntity,
)
from knowledge_index.connectors.entities._field import IndexField


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ClioMatterEntity(BaseEntity):
    """A Clio matter: the container a firm's documents are filed under."""

    matter_id: str = IndexField(..., description="Matter id", is_entity_id=True)
    display_number: str = IndexField(
        ..., description="Matter display number", is_name=True, embeddable=True
    )
    description: Optional[str] = IndexField(
        None, description="Matter description", embeddable=True
    )
    status: Optional[str] = IndexField(None, description="open/pending/closed")
    practice_area: Optional[str] = IndexField(
        None, description="Practice area name", embeddable=True
    )
    client_name: Optional[str] = IndexField(None, description="Client display name")
    created_at: Optional[datetime] = IndexField(None, description="Created", is_created_at=True)
    updated_at: Optional[datetime] = IndexField(None, description="Updated", is_updated_at=True)


class ClioDocumentEntity(FileEntity):
    """A document stored in Clio, filed under a matter (or contact/firm level)."""

    document_id: str = IndexField(..., description="Document id", is_entity_id=True)
    name: str = IndexField(..., description="Filename", is_name=True, embeddable=True)
    etag: Optional[str] = IndexField(
        None, description="Server change token; also the staging cache key."
    )
    matter_id: Optional[str] = IndexField(None, description="Matter the document is filed under")
    matter_display_number: Optional[str] = IndexField(
        None, description="Matter display number", embeddable=True
    )
    document_category: Optional[str] = IndexField(None, description="Clio document category")
    created_at: Optional[datetime] = IndexField(None, description="Created", is_created_at=True)
    updated_at: Optional[datetime] = IndexField(None, description="Updated", is_updated_at=True)

    @classmethod
    def from_api(
        cls,
        data: Dict[str, Any],
        *,
        api_base_url: str,
        breadcrumbs: list[Breadcrumb],
    ) -> Optional["ClioDocumentEntity"]:
        """Construct from a Clio ``documents.json`` record. Returns None for folders."""
        if str(data.get("type") or "").casefold() == "folder":
            return None
        identifier = data.get("id")
        filename = data.get("filename") or data.get("name")
        if identifier is None or not filename:
            return None
        matter = data.get("matter") or {}
        category = data.get("document_category") or {}
        return cls(
            document_id=str(identifier),
            breadcrumbs=breadcrumbs,
            name=str(filename),
            file_type=str(filename).rsplit(".", 1)[-1].lower() if "." in str(filename) else "",
            mime_type=data.get("content_type") or "application/octet-stream",
            size=data.get("size") or 0,
            url=f"{api_base_url}/documents/{identifier}/download.json",
            local_path=None,
            etag=data.get("etag"),
            matter_id=str(matter["id"]) if matter.get("id") is not None else None,
            matter_display_number=matter.get("display_number"),
            document_category=category.get("name"),
            created_at=_parse_dt(data.get("created_at")),
            updated_at=_parse_dt(data.get("updated_at")),
        )


class ClioDocumentDeletionEntity(DeletionEntity):
    """Deletion marker for a Clio document reported by ``include_deleted``."""

    deletes_entity_class = ClioDocumentEntity

    document_id: str = IndexField(
        ...,
        description="Id of the deleted document",
        is_entity_id=True,
        is_name=True,
    )
