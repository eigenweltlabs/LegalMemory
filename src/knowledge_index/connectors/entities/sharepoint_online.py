"""SharePoint Online Entities.

Entity hierarchy:
- SharePointOnlineSiteEntity (BaseEntity) - Sites
- SharePointOnlineDriveEntity (BaseEntity) - Document libraries (drives)
- SharePointOnlineItemEntity (BaseEntity) - List items
- SharePointOnlineFileEntity (FileEntity) - Files in drives
- SharePointOnlinePageEntity (BaseEntity) - Site pages

Deletion markers for incremental sync:
- SharePointOnlineFileDeletionEntity
- SharePointOnlineItemDeletionEntity
"""

from datetime import datetime
from typing import Any, Dict, Optional

from knowledge_index.connectors.entities._field import IndexField
from knowledge_index.connectors.entities._base import BaseEntity, DeletionEntity, FileEntity


class SharePointOnlineSiteEntity(BaseEntity):
    """SharePoint Online site."""

    site_id: str = IndexField(..., description="Graph site ID", is_entity_id=True)
    display_name: str = IndexField(
        ..., description="Site display name", is_name=True, embeddable=True
    )
    web_url: str = IndexField(..., description="Site web URL")
    description: Optional[str] = IndexField(
        None, description="Site description", embeddable=True
    )
    is_personal_site: bool = IndexField(
        False, description="Whether this is a OneDrive personal site"
    )
    created_at: Optional[datetime] = IndexField(
        None, description="Creation time", is_created_at=True
    )
    last_modified_at: Optional[datetime] = IndexField(
        None, description="Last modified time", is_updated_at=True
    )


class SharePointOnlineDriveEntity(BaseEntity):
    """SharePoint Online document library (drive)."""

    drive_id: str = IndexField(..., description="Graph drive ID", is_entity_id=True)
    name: str = IndexField(..., description="Drive name", is_name=True, embeddable=True)
    drive_type: str = IndexField("documentLibrary", description="Drive type")
    web_url: str = IndexField(..., description="Drive web URL")
    description: Optional[str] = IndexField(
        None, description="Drive description", embeddable=True
    )
    site_id: str = IndexField(..., description="Parent site ID")
    quota_total: Optional[int] = IndexField(None, description="Total quota in bytes")
    quota_used: Optional[int] = IndexField(None, description="Used quota in bytes")
    created_at: Optional[datetime] = IndexField(
        None, description="Creation time", is_created_at=True
    )
    last_modified_at: Optional[datetime] = IndexField(
        None, description="Last modified time", is_updated_at=True
    )


class SharePointOnlineItemEntity(BaseEntity):
    """SharePoint Online list item (non-file)."""

    spo_entity_id: str = IndexField(
        ...,
        description="Composite entity ID: spo:item:{site_id}:{list_id}:{item_id}",
        is_entity_id=True,
    )
    item_id: str = IndexField(..., description="Graph item ID")
    list_id: str = IndexField(..., description="Parent list ID")
    site_id: str = IndexField(..., description="Parent site ID")
    title: str = IndexField(..., description="Item title", is_name=True, embeddable=True)
    web_url: str = IndexField(..., description="Item web URL")
    content_type: Optional[str] = IndexField(None, description="Content type name")
    fields: Dict[str, Any] = IndexField(
        default_factory=dict,
        description="List item field values",
        embeddable=True,
    )
    created_at: Optional[datetime] = IndexField(
        None, description="Creation time", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="Last modified time", is_updated_at=True
    )


class SharePointOnlineFileEntity(FileEntity):
    """SharePoint Online file in a document library."""

    spo_entity_id: str = IndexField(
        ...,
        description="Composite entity ID: spo:file:{drive_id}:{item_id}",
        is_entity_id=True,
    )
    item_id: str = IndexField(..., description="Graph drive item ID")
    drive_id: str = IndexField(..., description="Parent drive ID")
    site_id: str = IndexField(..., description="Parent site ID")
    file_name: str = IndexField(
        ..., description="File name with extension", is_name=True, embeddable=True
    )
    web_url: str = IndexField(..., description="File web URL")
    download_url: Optional[str] = IndexField(
        None, description="Direct download URL", unhashable=True
    )
    parent_path: Optional[str] = IndexField(None, description="Parent folder path")
    # Graph's change tokens. Without one, staged content cannot be version-addressed and
    # every rescan re-downloads the whole library instead of just what changed.
    etag: Optional[str] = IndexField(None, description="Item eTag (content + metadata)")
    ctag: Optional[str] = IndexField(None, description="Item cTag (content only)")
    created_by: Optional[str] = IndexField(None, description="Created by user email/name")
    last_modified_by: Optional[str] = IndexField(
        None, description="Last modified by user email/name"
    )
    created_at: Optional[datetime] = IndexField(
        None, description="Creation time", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="Last modified time", is_updated_at=True
    )


class SharePointOnlinePageEntity(BaseEntity):
    """SharePoint Online site page."""

    page_id: str = IndexField(..., description="Graph page ID", is_entity_id=True)
    title: str = IndexField(..., description="Page title", is_name=True, embeddable=True)
    web_url: str = IndexField(..., description="Page web URL")
    description: Optional[str] = IndexField(
        None, description="Page description", embeddable=True
    )
    page_content: Optional[str] = IndexField(
        None, description="Page HTML content", embeddable=True
    )
    site_id: str = IndexField(..., description="Parent site ID")
    created_at: Optional[datetime] = IndexField(
        None, description="Creation time", is_created_at=True
    )
    updated_at: Optional[datetime] = IndexField(
        None, description="Last modified time", is_updated_at=True
    )


class SharePointOnlineFileDeletionEntity(DeletionEntity):
    """Deletion marker for a SharePoint Online file."""

    deletes_entity_class = SharePointOnlineFileEntity

    drive_id: str = IndexField(..., description="Drive ID of the deleted file")
    item_id: str = IndexField(..., description="Item ID of the deleted file", is_name=True)
    spo_entity_id: str = IndexField(
        ...,
        description="Entity ID of the deleted file",
        is_entity_id=True,
    )


class SharePointOnlineItemDeletionEntity(DeletionEntity):
    """Deletion marker for a SharePoint Online list item."""

    deletes_entity_class = SharePointOnlineItemEntity

    site_id: str = IndexField(..., description="Site ID of the deleted item")
    list_id: str = IndexField(..., description="List ID of the deleted item")
    item_id: str = IndexField(..., description="Item ID of the deleted item", is_name=True)
    spo_entity_id: str = IndexField(
        ...,
        description="Entity ID of the deleted item",
        is_entity_id=True,
    )
