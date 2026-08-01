"""Microsoft Purview sensitivity-label filtering for Graph-backed sources.

Used by the SharePoint Online and OneDrive connectors to skip
content carrying a configured set of Purview sensitivity label GUIDs.

Two filter points:

- **Container labels** on the M365 Group behind a SharePoint site/team
  (``group.assignedLabels``). Matched against the block list to skip an entire
  site without walking its drives.
- **Item labels** on individual files
  (``POST /drives/{drive-id}/items/{item-id}/extractSensitivityLabels``).
  Item labels do NOT inherit from container labels — they must be checked
  separately on every file.

Tenant-specific label GUIDs are configured by the user. Sublabels are NOT
expanded server-side; configure each label-to-block explicitly.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol

import httpx

from knowledge_index.connectors.runtime.logging import ContextualLogger


class _AsyncHttpClient(Protocol):
    """Structural type for the http client (covers httpx.AsyncClient and HttpClient)."""

    async def get(self, url: str, **kwargs: Any) -> httpx.Response: ...
    async def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class SensitivityLabelFilter:
    """Applies a Purview sensitivity-label block list to Graph driveItems.

    The filter is stateless apart from the configured block set; it makes
    Graph calls through the caller-supplied token + http_client to inherit
    the source's retry/throttle behavior.
    """

    def __init__(
        self,
        *,
        excluded_label_ids: Iterable[str],
        skip_encrypted: bool,
        skip_unlabeled: bool,
        http_client: _AsyncHttpClient,
        token_provider: Callable[[], Awaitable[str]],
        logger: ContextualLogger,
    ) -> None:
        """Build a filter with the configured block list and behavior flags."""
        self._excluded: set[str] = {
            cleaned for lid in excluded_label_ids if lid and (cleaned := lid.strip().lower())
        }
        self._skip_encrypted = skip_encrypted
        self._skip_unlabeled = skip_unlabeled
        self._http_client = http_client
        self._token_provider = token_provider
        self._logger = logger

    @property
    def enabled(self) -> bool:
        """True if any label-based filtering is configured."""
        return bool(self._excluded) or self._skip_unlabeled

    async def should_skip_site(self, *, site_id: str, group_id: Optional[str]) -> bool:
        """Check the container label on the site's underlying M365 Group.

        Returns True if any ``assignedLabel`` GUID is in the block list. A
        site without a backing group, or one whose labels we cannot read,
        is never short-circuited here — per-file checks still run.
        """
        if not self._excluded:
            return False
        if not group_id:
            return False

        try:
            labels = await self._fetch_group_assigned_labels(group_id)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                f"Could not read assignedLabels for group {group_id} "
                f"(site {site_id}): {exc}. Falling back to per-file checks."
            )
            return False

        matched = next(
            (lid for lid in labels if lid and lid.lower() in self._excluded),
            None,
        )
        if matched:
            self._logger.info(
                f"Skipping site {site_id}: container label {matched} is in block list."
            )
            return True
        return False

    async def should_skip_item(
        self,
        *,
        drive_id: str,
        item_id: str,
        item_name: str = "",
    ) -> bool:
        """Check the per-file sensitivity label via extractSensitivityLabels.

        Returns True if any label on the file is in the block list, if the
        file is label-encrypted and ``skip_encrypted`` is set, or if the
        file is unlabeled and ``skip_unlabeled`` is set.
        """
        if not self.enabled:
            return False

        try:
            labels = await self._extract_item_labels(drive_id, item_id)
        except _EncryptedFileError:
            if self._skip_encrypted:
                self._logger.info(
                    f"Skipping encrypted file {item_name or item_id} in drive {drive_id}."
                )
                return True
            raise
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                f"Could not read sensitivity labels for {item_name or item_id} "
                f"in drive {drive_id}: {exc}. Indexing without label filter."
            )
            return False

        if not labels:
            if self._skip_unlabeled:
                self._logger.info(
                    f"Skipping unlabeled file {item_name or item_id} in drive {drive_id}."
                )
                return True
            return False

        matched = next(
            (lid for lid in labels if lid and lid.lower() in self._excluded),
            None,
        )
        if matched:
            self._logger.info(
                f"Skipping file {item_name or item_id} in drive {drive_id}: "
                f"item label {matched} is in block list."
            )
            return True
        return False

    async def _fetch_group_assigned_labels(self, group_id: str) -> list[str]:
        url = f"{GRAPH_BASE_URL}/groups/{group_id}"
        params = {"$select": "assignedLabels"}
        data = await self._graph_get(url, params=params)
        labels = data.get("assignedLabels") or []
        return [
            entry.get("labelId", "")
            for entry in labels
            if isinstance(entry, dict) and entry.get("labelId")
        ]

    async def _extract_item_labels(self, drive_id: str, item_id: str) -> list[str]:
        url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/extractSensitivityLabels"
        data = await self._graph_post(url)
        labels = data.get("labels") or []
        return [
            entry.get("sensitivityLabelId", "")
            for entry in labels
            if isinstance(entry, dict) and entry.get("sensitivityLabelId")
        ]

    async def _graph_get(
        self, url: str, *, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        token = await self._token_provider()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = await self._http_client.get(url, headers=headers, params=params)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    async def _graph_post(self, url: str) -> dict[str, Any]:
        token = await self._token_provider()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = await self._http_client.post(url, headers=headers)
        if response.status_code == 423:
            raise _EncryptedFileError(url)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


class _EncryptedFileError(Exception):
    """Raised when extractSensitivityLabels returns 423 Locked (encrypted file)."""

    def __init__(self, url: str) -> None:
        super().__init__(f"Encrypted file (423 Locked) at {url}")
