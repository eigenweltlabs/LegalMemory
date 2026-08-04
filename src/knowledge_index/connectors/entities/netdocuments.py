"""NetDocuments entity schemas.

A NetDocuments repository nests cabinet → workspace → folder → document. Only the
document carries bytes; the containers above it exist so a document can say which
matter it belongs to, which is what makes a retrieved paragraph citable.

Identifiers are the repository's own and keep their prefixes: cabinets start ``NG-``,
groups ``UG-``, and documents and folders are 12-character ids. They are carried as
strings rather than parsed, because the prefix is the only thing that distinguishes
them and losing it makes an id unusable against the API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import computed_field

from knowledge_index.connectors.entities._base import BaseEntity, Breadcrumb, DeletionEntity, FileEntity
from knowledge_index.connectors.entities._field import IndexField


class NetDocumentsCabinetEntity(BaseEntity):
    """A cabinet: the top-level security and organizational boundary in a repository."""

    cabinet_id: str = IndexField(..., description="NetDocuments cabinet ID (NG-…)", is_entity_id=True)
    name: str = IndexField(..., description="Cabinet name", is_name=True, embeddable=True)
    created_at: Optional[datetime] = IndexField(
        None, description="When the cabinet was created", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="When the cabinet was last modified", is_updated_at=True
    )

    repository_id: Optional[str] = IndexField(
        None, description="Repository the cabinet belongs to", embeddable=False
    )
    description: Optional[str] = IndexField(
        None, description="Cabinet description", embeddable=True
    )

    @classmethod
    def from_api(cls, data: Dict[str, Any], *, repository_id: str | None = None) -> Optional[NetDocumentsCabinetEntity]:
        """Build from a ``/v1/cabinet/{id}/info`` payload."""
        identifier = data.get("id") or data.get("envId") or data.get("cabinetId")
        if not identifier:
            return None
        return cls(
            cabinet_id=str(identifier),
            breadcrumbs=[],
            name=str(data.get("name") or identifier),
            description=data.get("description"),
            repository_id=repository_id or data.get("repository"),
            created_at=data.get("created"),
            updated_at=data.get("modified"),
        )


class NetDocumentsFolderEntity(BaseEntity):
    """A container inside a cabinet: a workspace, a folder, or a saved filter.

    NetDocuments models all three as containers addressable by the same endpoint, so
    they are one entity type distinguished by ``container_type`` rather than three
    near-identical classes.
    """

    folder_id: str = IndexField(..., description="NetDocuments container ID", is_entity_id=True)
    name: str = IndexField(..., description="Container name", is_name=True, embeddable=True)
    created_at: Optional[datetime] = IndexField(
        None, description="When the container was created", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="When the container was last modified", is_updated_at=True
    )

    container_type: Optional[str] = IndexField(
        None, description="workspace, folder or filter", embeddable=True
    )
    cabinet_id: Optional[str] = IndexField(
        None, description="Cabinet this container sits in", embeddable=False
    )
    parent_id: Optional[str] = IndexField(
        None, description="Parent container ID", embeddable=False
    )
    client_name: Optional[str] = IndexField(
        None, description="Client the workspace belongs to, when profiled", embeddable=True
    )
    matter_name: Optional[str] = IndexField(
        None, description="Matter the workspace represents, when profiled", embeddable=True
    )

    @classmethod
    def from_api(
        cls,
        data: Dict[str, Any],
        *,
        cabinet_id: str | None = None,
        parent_id: str | None = None,
        breadcrumbs: List[Breadcrumb] | None = None,
    ) -> Optional[NetDocumentsFolderEntity]:
        """Build from a container listing entry or ``/v1/Folder/{id}/info`` payload."""
        identifier = data.get("id") or data.get("envId")
        if not identifier:
            return None
        return cls(
            folder_id=str(identifier),
            breadcrumbs=breadcrumbs or [],
            name=str(data.get("name") or identifier),
            container_type=str(data.get("type") or "folder").lower(),
            cabinet_id=cabinet_id,
            parent_id=parent_id,
            client_name=data.get("client"),
            matter_name=data.get("matter"),
            created_at=data.get("created"),
            updated_at=data.get("modified"),
        )


class NetDocumentsDocumentEntity(FileEntity):
    """A document: the only NetDocuments object that carries bytes to index."""

    document_id: str = IndexField(..., description="NetDocuments document ID", is_entity_id=True)
    name: str = IndexField(..., description="Document name", is_name=True, embeddable=True)
    created_at: Optional[datetime] = IndexField(
        None, description="When the document was created", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="When the document was last modified", is_updated_at=True
    )

    extension: Optional[str] = IndexField(None, description="File extension", embeddable=False)
    version: Optional[str] = IndexField(None, description="Version label", embeddable=False)
    cabinet_id: Optional[str] = IndexField(
        None, description="Cabinet the document sits in", embeddable=False
    )
    folder_id: Optional[str] = IndexField(
        None, description="Container the document was found in", embeddable=False
    )
    client_name: Optional[str] = IndexField(
        None, description="Client from the document profile", embeddable=True
    )
    matter_name: Optional[str] = IndexField(
        None, description="Matter from the document profile", embeddable=True
    )
    document_type: Optional[str] = IndexField(
        None, description="Document type from the profile", embeddable=True
    )
    author: Optional[str] = IndexField(
        None, description="Author from the profile", embeddable=True
    )

    @classmethod
    def from_api(
        cls,
        data: Dict[str, Any],
        *,
        api_base_url: str,
        cabinet_id: str | None = None,
        folder_id: str | None = None,
        breadcrumbs: List[Breadcrumb] | None = None,
    ) -> Optional[NetDocumentsDocumentEntity]:
        """Build from a container listing entry or ``/v1/Document/{id}/info`` payload.

        Returns ``None`` for a payload with no id: a document that cannot be addressed
        cannot be fetched or re-fetched, and carrying it forward would produce an index
        row that can never be refreshed or deleted.
        """
        identifier = data.get("id") or data.get("envId")
        if not identifier:
            return None
        document_id = str(identifier)
        extension = str(data.get("extension") or "").lstrip(".").lower()
        size = data.get("size")
        return cls(
            document_id=document_id,
            breadcrumbs=breadcrumbs or [],
            name=str(data.get("name") or document_id),
            url=f"{api_base_url}/v1/Document/{document_id}",
            size=int(size) if isinstance(size, (int, float, str)) and str(size).isdigit() else 0,
            file_type=extension or "file",
            mime_type=data.get("mimeType"),
            local_path=None,
            extension=extension or None,
            version=str(data.get("version")) if data.get("version") is not None else None,
            cabinet_id=cabinet_id,
            folder_id=folder_id,
            client_name=data.get("client"),
            matter_name=data.get("matter"),
            document_type=data.get("documentType") or data.get("docType"),
            author=data.get("author"),
            created_at=data.get("created"),
            updated_at=data.get("modified"),
        )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Deep link a lawyer can open in ndWeb to see the document in its workspace."""
        return f"https://vault.netvoyage.com/neWeb2/docCenter.aspx?id={self.document_id}"


class NetDocumentsDocumentDeletionEntity(DeletionEntity):
    """A document that has left the repository, or left the synced scope."""

    deletes_entity_class = NetDocumentsDocumentEntity

    document_id: str = IndexField(
        ...,
        description="NetDocuments document ID that was removed",
        is_entity_id=True,
        is_name=True,
    )
