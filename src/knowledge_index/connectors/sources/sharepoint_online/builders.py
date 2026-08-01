"""Entity builders for SharePoint Online.

Functions that build typed entity objects from Microsoft Graph API responses.
Each builder validates required fields and extracts access control.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from knowledge_index.connectors.runtime.errors import EntityProcessingError
from knowledge_index.connectors.entities._base import AccessControl, Breadcrumb
from knowledge_index.connectors.entities.sharepoint_online import (
    SharePointOnlineDriveEntity,
    SharePointOnlineFileEntity,
    SharePointOnlinePageEntity,
    SharePointOnlineSiteEntity,
)
from knowledge_index.connectors.sources.sharepoint_online.acl import extract_access_control


def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def build_site_entity(
    site_data: Dict[str, Any],
    breadcrumbs: List[Breadcrumb],
    access: Optional[AccessControl] = None,
) -> SharePointOnlineSiteEntity:
    """Build a site entity from Graph API site data."""
    site_id = site_data.get("id")
    if not site_id:
        raise EntityProcessingError("Missing id for site")

    display_name = site_data.get("displayName")
    if not display_name:
        raise EntityProcessingError(f"Missing displayName for site {site_id}")

    web_url = site_data.get("webUrl", "")

    return SharePointOnlineSiteEntity(
        site_id=site_id,
        display_name=display_name,
        web_url=web_url,
        description=site_data.get("description"),
        is_personal_site=site_data.get("isPersonalSite", False),
        created_at=_parse_datetime(site_data.get("createdDateTime")),
        last_modified_at=_parse_datetime(site_data.get("lastModifiedDateTime")),
        breadcrumbs=breadcrumbs,
        access=access,
    )


async def build_drive_entity(
    drive_data: Dict[str, Any],
    site_id: str,
    breadcrumbs: List[Breadcrumb],
    access: Optional[AccessControl] = None,
) -> SharePointOnlineDriveEntity:
    """Build a drive entity from Graph API drive data."""
    drive_id = drive_data.get("id")
    if not drive_id:
        raise EntityProcessingError("Missing id for drive")

    name = drive_data.get("name")
    if not name:
        raise EntityProcessingError(f"Missing name for drive {drive_id}")

    quota = drive_data.get("quota", {}) or {}

    return SharePointOnlineDriveEntity(
        drive_id=drive_id,
        name=name,
        drive_type=drive_data.get("driveType", "documentLibrary"),
        web_url=drive_data.get("webUrl", ""),
        description=drive_data.get("description"),
        site_id=site_id,
        quota_total=quota.get("total"),
        quota_used=quota.get("used"),
        created_at=_parse_datetime(drive_data.get("createdDateTime")),
        last_modified_at=_parse_datetime(drive_data.get("lastModifiedDateTime")),
        breadcrumbs=breadcrumbs,
        access=access,
    )


async def build_file_entity(
    item_data: Dict[str, Any],
    drive_id: str,
    site_id: str,
    breadcrumbs: List[Breadcrumb],
    permissions: Optional[List[Dict[str, Any]]] = None,
    sp_unique_id: Optional[str] = None,
) -> SharePointOnlineFileEntity:
    """Build a file entity from Graph API drive item data.

    Args:
        item_data: Graph drive item dict.
        drive_id: Drive ID containing the item.
        site_id: Site ID the drive belongs to.
        breadcrumbs: Hierarchy breadcrumbs.
        permissions: Optional permissions list from
            ``/drives/{id}/items/{id}/permissions``.
        sp_unique_id: Optional SharePoint ``listItemUniqueId`` for the item.
            Required to translate sharing-link permissions; the caller should
            fetch it via :meth:`GraphClient.get_item_sp_unique_id` when any
            of the item's permissions has a ``link`` block.
    """
    item_id = item_data.get("id")
    if not item_id:
        raise EntityProcessingError("Missing id for file item")

    file_name = item_data.get("name")
    if not file_name:
        raise EntityProcessingError(f"Missing name for file item {item_id}")

    file_obj = item_data.get("file")
    if not file_obj:
        raise EntityProcessingError(f"Item {item_id} is not a file")

    size = item_data.get("size", 0)
    mime_type = file_obj.get("mimeType", "")

    if "." in file_name:
        file_ext = file_name.rsplit(".", 1)[-1].lower()
    else:
        file_ext = ""

    parent_ref = item_data.get("parentReference", {}) or {}
    parent_path = parent_ref.get("path", "")

    created_by_obj = item_data.get("createdBy", {}) or {}
    created_by_user = created_by_obj.get("user", {}) or {}
    created_by = created_by_user.get("email") or created_by_user.get("displayName")

    modified_by_obj = item_data.get("lastModifiedBy", {}) or {}
    modified_by_user = modified_by_obj.get("user", {}) or {}
    last_modified_by = modified_by_user.get("email") or modified_by_user.get("displayName")

    download_url = item_data.get("@microsoft.graph.downloadUrl", "")
    spo_entity_id = f"spo:file:{drive_id}:{item_id}"

    access = await extract_access_control(permissions or [], sp_unique_id) if permissions else None

    return SharePointOnlineFileEntity(
        url=download_url or item_data.get("webUrl", ""),
        size=size,
        file_type=file_ext,
        mime_type=mime_type,
        name=file_name,
        spo_entity_id=spo_entity_id,
        item_id=item_id,
        drive_id=drive_id,
        site_id=site_id,
        file_name=file_name,
        web_url=item_data.get("webUrl", ""),
        download_url=download_url,
        parent_path=parent_path,
        # cTag tracks content changes specifically; eTag also moves on metadata edits.
        etag=item_data.get("eTag"),
        ctag=item_data.get("cTag"),
        created_by=created_by,
        last_modified_by=last_modified_by,
        created_at=_parse_datetime(item_data.get("createdDateTime")),
        updated_at=_parse_datetime(item_data.get("lastModifiedDateTime")),
        access=access,
        breadcrumbs=breadcrumbs,
    )


async def build_page_entity(
    page_data: Dict[str, Any],
    site_id: str,
    breadcrumbs: List[Breadcrumb],
    access: Optional[AccessControl] = None,
) -> SharePointOnlinePageEntity:
    """Build a page entity from Graph API site page data."""
    page_id = page_data.get("id")
    if not page_id:
        raise EntityProcessingError("Missing id for page")

    title = page_data.get("title")
    if not title:
        raise EntityProcessingError(f"Missing title for page {page_id}")

    return SharePointOnlinePageEntity(
        page_id=page_id,
        title=title,
        web_url=page_data.get("webUrl", ""),
        description=page_data.get("description"),
        page_content=json.dumps(page_data["webParts"]) if page_data.get("webParts") else None,
        site_id=site_id,
        created_at=_parse_datetime(page_data.get("createdDateTime")),
        updated_at=_parse_datetime(page_data.get("lastModifiedDateTime")),
        breadcrumbs=breadcrumbs,
        access=access,
    )
