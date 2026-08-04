"""iManage Work entity schemas.

An iManage estate nests library → workspace → folder → document. The workspace is the
matter: for a law firm it is the unit a partner reasons about, and it is what gives a
retrieved paragraph something citable to point at.

Identifiers keep the source's own composite form — ``ACTIVE_US!4512345.1`` is library,
document number and version in one string. They are carried whole rather than split,
because that composite is what every later API call takes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import computed_field

from knowledge_index.connectors.entities._base import (
    BaseEntity,
    Breadcrumb,
    DeletionEntity,
    FileEntity,
)
from knowledge_index.connectors.entities._field import IndexField


class IManageWorkspaceEntity(BaseEntity):
    """A workspace: in a law firm, the matter."""

    workspace_id: str = IndexField(..., description="iManage workspace ID", is_entity_id=True)
    name: str = IndexField(..., description="Workspace name", is_name=True, embeddable=True)
    created_at: Optional[datetime] = IndexField(
        None, description="When the workspace was created", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="When the workspace was last modified", is_updated_at=True
    )

    library_id: Optional[str] = IndexField(
        None, description="Library the workspace lives in", embeddable=False
    )
    description: Optional[str] = IndexField(
        None, description="Workspace description", embeddable=True
    )
    owner: Optional[str] = IndexField(None, description="Workspace owner", embeddable=True)
    subclass: Optional[str] = IndexField(
        None, description="Workspace subclass, often the practice area", embeddable=True
    )
    client_id: Optional[str] = IndexField(
        None, description="Client from custom1, the firm's conventional client field",
        embeddable=True,
    )
    matter_id: Optional[str] = IndexField(
        None, description="Matter from custom2, the firm's conventional matter field",
        embeddable=True,
    )

    @classmethod
    def from_api(
        cls, data: Dict[str, Any], *, library_id: str | None = None
    ) -> Optional[IManageWorkspaceEntity]:
        """Build from a workspace profile payload."""
        identifier = data.get("id")
        if not identifier:
            return None
        return cls(
            workspace_id=str(identifier),
            breadcrumbs=[],
            name=str(data.get("name") or identifier),
            library_id=library_id or data.get("database"),
            description=data.get("description"),
            owner=data.get("owner_description") or data.get("owner"),
            subclass=data.get("subclass_description") or data.get("subclass"),
            # custom1/custom2 are where firms conventionally put client and matter.
            # The description variants carry the human-readable value.
            client_id=data.get("custom1_description") or data.get("custom1"),
            matter_id=data.get("custom2_description") or data.get("custom2"),
            created_at=data.get("create_date"),
            updated_at=data.get("edit_date"),
        )


class IManageFolderEntity(BaseEntity):
    """A folder inside a workspace."""

    folder_id: str = IndexField(..., description="iManage folder ID", is_entity_id=True)
    name: str = IndexField(..., description="Folder name", is_name=True, embeddable=True)
    created_at: Optional[datetime] = IndexField(
        None, description="When the folder was created", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="When the folder was last modified", is_updated_at=True
    )

    library_id: Optional[str] = IndexField(
        None, description="Library the folder lives in", embeddable=False
    )
    workspace_id: Optional[str] = IndexField(
        None, description="Workspace the folder belongs to", embeddable=False
    )
    parent_id: Optional[str] = IndexField(None, description="Parent folder ID", embeddable=False)
    description: Optional[str] = IndexField(
        None, description="Folder description", embeddable=True
    )
    folder_type: Optional[str] = IndexField(
        None, description="Folder type reported by iManage", embeddable=False
    )
    owner: Optional[str] = IndexField(None, description="Folder owner", embeddable=True)

    @classmethod
    def from_api(
        cls,
        data: Dict[str, Any],
        *,
        library_id: str | None = None,
        breadcrumbs: List[Breadcrumb] | None = None,
    ) -> Optional[IManageFolderEntity]:
        """Build from a folder profile payload."""
        identifier = data.get("id")
        if not identifier:
            return None
        return cls(
            folder_id=str(identifier),
            breadcrumbs=breadcrumbs or [],
            name=str(data.get("name") or identifier),
            library_id=library_id or data.get("database"),
            workspace_id=data.get("workspace_id"),
            parent_id=data.get("parent_id"),
            description=data.get("description"),
            folder_type=data.get("folder_type"),
            owner=data.get("owner_description") or data.get("owner"),
            updated_at=data.get("edit_date"),
        )


class IManageDocumentEntity(FileEntity):
    """A document: the only iManage object that carries bytes to index."""

    document_id: str = IndexField(
        ..., description="iManage document ID (library!number.version)", is_entity_id=True
    )
    name: str = IndexField(..., description="Document name", is_name=True, embeddable=True)
    created_at: Optional[datetime] = IndexField(
        None, description="When the document was created", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="When the document was last edited", is_updated_at=True
    )

    library_id: Optional[str] = IndexField(
        None, description="Library the document lives in", embeddable=False
    )
    document_number: Optional[str] = IndexField(
        None, description="iManage document number", embeddable=False
    )
    version: Optional[str] = IndexField(None, description="Version number", embeddable=False)
    extension: Optional[str] = IndexField(None, description="File extension", embeddable=False)
    workspace_id: Optional[str] = IndexField(
        None, description="Workspace the document belongs to", embeddable=False
    )
    workspace_name: Optional[str] = IndexField(
        None, description="Workspace name, the matter in a law firm", embeddable=True
    )
    folder_id: Optional[str] = IndexField(
        None, description="Folder the document was found in", embeddable=False
    )
    author: Optional[str] = IndexField(None, description="Document author", embeddable=True)
    operator: Optional[str] = IndexField(
        None, description="Person who last operated on the document", embeddable=True
    )
    document_class: Optional[str] = IndexField(
        None, description="iManage class, e.g. correspondence or pleading", embeddable=True
    )
    subclass: Optional[str] = IndexField(
        None, description="iManage subclass", embeddable=True
    )
    is_declared_record: Optional[bool] = IndexField(
        None, description="Whether the document is declared as a record", embeddable=False
    )

    @classmethod
    def from_api(
        cls,
        data: Dict[str, Any],
        *,
        api_base_url: str,
        customer_id: str,
        library_id: str | None = None,
        folder_id: str | None = None,
        breadcrumbs: List[Breadcrumb] | None = None,
    ) -> Optional[IManageDocumentEntity]:
        """Build from a document profile payload.

        Returns ``None`` for a payload with no id: a document that cannot be addressed
        cannot be re-fetched or deleted later, so indexing it would create a row that
        can never be corrected.
        """
        identifier = data.get("id")
        if not identifier:
            return None
        document_id = str(identifier)
        library = str(library_id or data.get("database") or document_id.partition("!")[0])
        size = data.get("size")
        extension = str(data.get("extension") or "").lstrip(".").lower()
        return cls(
            document_id=document_id,
            breadcrumbs=breadcrumbs or [],
            name=str(data.get("name") or data.get("full_file_name") or document_id),
            url=(
                f"{api_base_url}/work/api/v2/customers/{customer_id}/libraries/"
                f"{library}/documents/{document_id}/download"
            ),
            size=int(size) if isinstance(size, (int, float)) or str(size or "").isdigit() else 0,
            file_type=extension or "file",
            mime_type=data.get("content_type"),
            local_path=None,
            library_id=library,
            document_number=(
                str(data["document_number"]) if data.get("document_number") is not None else None
            ),
            version=str(data["version"]) if data.get("version") is not None else None,
            extension=extension or None,
            workspace_id=data.get("workspace_id"),
            workspace_name=data.get("workspace_name"),
            folder_id=folder_id,
            author=data.get("author_description") or data.get("author"),
            operator=data.get("operator_description") or data.get("operator"),
            document_class=data.get("class_description") or data.get("class"),
            subclass=data.get("subclass_description") or data.get("subclass"),
            is_declared_record=data.get("is_declared"),
            created_at=data.get("create_date"),
            updated_at=data.get("edit_date"),
        )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Deep link a lawyer can open in iManage Work to see the document in context."""
        return f"iwl://open?id={self.document_id}"


class IManageDocumentDeletionEntity(DeletionEntity):
    """A document that has left iManage, or left the synced scope."""

    deletes_entity_class = IManageDocumentEntity

    document_id: str = IndexField(
        ...,
        description="iManage document ID that was removed",
        is_entity_id=True,
        is_name=True,
    )
