"""Outlook Calendar source implementation.

Comprehensive implementation that retrieves:
  - Calendars (GET /me/calendars)
  - Events (GET /me/calendars/{calendar_id}/events)
  - Event attachments (GET /me/events/{event_id}/attachments)

Follows the same structure as the Gmail and Outlook Mail implementations.
"""

import base64
from typing import Any, AsyncGenerator, Dict, List, Optional

from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.types import RateLimitLevel
from knowledge_index.connectors.runtime.types import NodeSelectionData
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.errors import FileSkippedException
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.configs import OutlookCalendarConfig
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.acl import graph_owner_email, owner_to_access
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.outlook_calendar import (
    OutlookCalendarAttachmentEntity,
    OutlookCalendarCalendarEntity,
    OutlookCalendarEventEntity,
)
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.types import AuthenticationMethod, OAuthType


@source(
    name="Outlook Calendar",
    short_name="outlook_calendar",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_REFRESH,
    auth_config_class=None,
    config_class=OutlookCalendarConfig,
    labels=["Productivity", "Calendar"],
    supports_continuous=False,
    supports_access_control=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class OutlookCalendarSource(BaseSource):
    """Outlook Calendar source connector integrates with the Microsoft Graph API to extract data.

    Synchronizes data from Outlook calendars.

    It provides comprehensive access to calendars, events, and attachments
    with proper timezone handling and meeting management features.

    A calendar read through ``/me`` is an owner-scoped corpus: its events belong to one
    person, and that is a real access control rather than the absence of one. Every entity
    is therefore indexed with the authenticated account as its only viewer, resolved once
    per run.
    """

    GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: OutlookCalendarConfig,
    ) -> "OutlookCalendarSource":
        """Create a new Outlook Calendar source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        # Resolved once per run: the calendar owner does not change mid-sync, and a /me
        # call per event would spend quota to learn the same address repeatedly.
        instance._owner_email: Optional[str] = None
        instance._owner_resolved = False
        return instance

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    async def _owner_access(self) -> Optional[AccessControl]:
        """Who may read this calendar: its owner, and nobody else.

        A calendar is not an object with a permission list — it is one person's schedule,
        including meetings whose subjects name matters and counterparties. Mirroring that
        as a single viewer is the accurate ACL, not a stand-in for one.

        Returns None when the owner cannot be resolved. None means "unknown" and keeps the
        events fail-closed until an administrator grants access at the project level; an
        empty AccessControl would assert that nobody may read a calendar in active use.
        """
        if not self._owner_resolved:
            self._owner_resolved = True
            me: Optional[dict] = None
            try:
                me = await self._get(f"{self.GRAPH_BASE_URL}/me")
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(
                    f"Could not resolve the calendar owner ({e}); events are indexed with "
                    "unknown access (fail-closed)."
                )
            self._owner_email = graph_owner_email(me)
            if me is not None and not self._owner_email:
                self.logger.warning(
                    "Microsoft Graph returned no address for the signed-in account; events "
                    "are indexed with unknown access (fail-closed)."
                )
        return owner_to_access(self._owner_email)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _authed_headers(self) -> Dict[str, str]:
        """Build Authorization + Accept headers with a fresh token."""
        token = await self.auth.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    async def _refresh_and_get_headers(self) -> Dict[str, str]:
        """Force-refresh the token and return updated headers."""
        new_token = await self.auth.force_refresh()
        return {
            "Authorization": f"Bearer {new_token}",
            "Accept": "application/json",
        }

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _get(self, url: str, params: Optional[dict] = None) -> Any:
        """Make an authenticated GET request to Microsoft Graph API."""
        self.logger.debug(f"Making authenticated GET request to: {url} with params: {params}")

        headers = await self._authed_headers()
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401 and self.auth.supports_refresh:
            self.logger.warning(
                f"Got 401 Unauthorized from Microsoft Graph API at {url}, refreshing token..."
            )
            headers = await self._refresh_and_get_headers()
            response = await self.http_client.get(url, headers=headers, params=params)

        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
        )
        return response.json()

    # ------------------------------------------------------------------
    # Entity generators
    # ------------------------------------------------------------------

    async def _generate_calendar_entities(
        self,
    ) -> AsyncGenerator[OutlookCalendarCalendarEntity, None]:
        """Generate OutlookCalendarCalendarEntity objects for each calendar.

        Endpoint: GET /me/calendars
        """
        self.logger.info("Starting calendar entity generation")
        url = f"{self.GRAPH_BASE_URL}/me/calendars"
        calendar_count = 0

        try:
            while url:
                self.logger.debug(f"Fetching calendars from: {url}")
                data = await self._get(url)
                calendars = data.get("value", [])
                self.logger.info(f"Retrieved {len(calendars)} calendars")

                for calendar_data in calendars:
                    calendar_count += 1
                    calendar_id = calendar_data["id"]
                    calendar_name = calendar_data.get("name", "Unknown Calendar")

                    self.logger.debug(f"Processing calendar #{calendar_count}: {calendar_name}")

                    yield OutlookCalendarCalendarEntity(
                        breadcrumbs=[],
                        id=calendar_id,
                        name=calendar_name,
                        created_at=None,
                        updated_at=None,
                        color=calendar_data.get("color"),
                        hex_color=calendar_data.get("hexColor"),
                        change_key=calendar_data.get("changeKey"),
                        can_edit=calendar_data.get("canEdit", False),
                        can_share=calendar_data.get("canShare", False),
                        can_view_private_items=calendar_data.get("canViewPrivateItems", False),
                        is_default_calendar=calendar_data.get("isDefaultCalendar", False),
                        is_removable=calendar_data.get("isRemovable", True),
                        is_tallying_responses=calendar_data.get("isTallyingResponses", False),
                        owner=calendar_data.get("owner"),
                        allowed_online_meeting_providers=calendar_data.get(
                            "allowedOnlineMeetingProviders", []
                        ),
                        default_online_meeting_provider=calendar_data.get(
                            "defaultOnlineMeetingProvider"
                        ),
                        web_url_override=calendar_data.get("webUrl"),
                    )

                url = data.get("@odata.nextLink")
                if url:
                    self.logger.debug("Following pagination to next page")

            self.logger.info(f"Completed calendar generation. Total calendars: {calendar_count}")

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating calendar entities: {str(e)}")
            raise

    async def _generate_event_entities(
        self,
        calendar: OutlookCalendarCalendarEntity,
        files: FileService | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate OutlookCalendarEventEntity objects and their attachments.

        Endpoint: GET /me/calendars/{calendar_id}/events
        """
        calendar_id = calendar.id
        calendar_name = calendar.name
        self.logger.info(f"Starting event generation for calendar: {calendar_name}")

        url = f"{self.GRAPH_BASE_URL}/me/calendars/{calendar_id}/events"
        params: dict | None = {"$top": 50}
        event_count = 0

        cal_breadcrumb = Breadcrumb(
            entity_id=calendar_id,
            name=calendar_name,
            entity_type="OutlookCalendarCalendarEntity",
        )

        try:
            while url:
                self.logger.debug(f"Fetching events from: {url}")
                data = await self._get(url, params=params)
                events = data.get("value", [])
                self.logger.info(f"Retrieved {len(events)} events from calendar {calendar_name}")

                for event_data in events:
                    event_count += 1
                    event_id = event_data.get("id", "unknown")
                    event_subject = event_data.get("subject", f"Event {event_count}")

                    if event_data.get("isCancelled"):
                        self.logger.info(f"Skipping cancelled event: {event_subject}")
                        continue

                    self.logger.debug(f"Processing event #{event_count}: {event_subject}")

                    try:
                        async for entity in self._process_event(
                            event_data, cal_breadcrumb, files=files
                        ):
                            yield entity
                    except SourceAuthError:
                        raise
                    except Exception as e:
                        self.logger.warning(f"Error processing event {event_id}: {str(e)}")

                url = data.get("@odata.nextLink")
                if url:
                    self.logger.debug("Following pagination to next page")
                    params = None

            self.logger.info(
                f"Completed event generation for calendar {calendar_name}. "
                f"Total events: {event_count}"
            )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating events for calendar {calendar_name}: {str(e)}")
            raise

    async def _process_event(
        self,
        event_data: Dict,
        cal_breadcrumb: Breadcrumb,
        files: FileService | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Process a single event and its attachments."""
        event_id = event_data["id"]
        event_subject = event_data.get("subject", "No Subject")

        self.logger.debug(f"Processing event: {event_subject} (ID: {event_id})")

        event_entity = OutlookCalendarEventEntity.from_api(
            event_data, cal_breadcrumb=cal_breadcrumb
        )
        yield event_entity
        self.logger.debug(f"Event entity yielded for {event_subject}")

        event_breadcrumb = Breadcrumb(
            entity_id=event_id,
            name=event_subject,
            entity_type="OutlookCalendarEventEntity",
        )

        if event_entity.has_attachments:
            self.logger.debug(f"Event {event_subject} has attachments, processing them")
            attachment_count = 0
            try:
                async for attachment_entity in self._process_event_attachments(
                    event_id,
                    [cal_breadcrumb, event_breadcrumb],
                    event_entity.web_url,
                    files=files,
                ):
                    attachment_count += 1
                    self.logger.debug(
                        f"Yielding attachment #{attachment_count} from event {event_subject}"
                    )
                    yield attachment_entity
                self.logger.debug(
                    f"Processed {attachment_count} attachments for event {event_subject}"
                )
            except SourceAuthError:
                raise
            except Exception as e:
                self.logger.warning(f"Error processing attachments for event {event_id}: {str(e)}")

    async def _process_event_attachments(
        self,
        event_id: str,
        breadcrumbs: List[Breadcrumb],
        event_web_url: Optional[str],
        files: FileService | None = None,
    ) -> AsyncGenerator[OutlookCalendarAttachmentEntity, None]:
        """Process event attachments using the standard file processing pipeline."""
        self.logger.debug(f"Processing attachments for event {event_id}")

        url: str | None = f"{self.GRAPH_BASE_URL}/me/events/{event_id}/attachments"

        try:
            while url:
                self.logger.debug(f"Making request to: {url}")
                data = await self._get(url)
                attachments = data.get("value", [])
                self.logger.debug(f"Retrieved {len(attachments)} attachments for event {event_id}")

                for att_idx, attachment in enumerate(attachments):
                    processed_entity = await self._process_single_attachment(
                        attachment,
                        event_id,
                        breadcrumbs,
                        att_idx,
                        len(attachments),
                        event_web_url,
                        files=files,
                    )
                    if processed_entity:
                        yield processed_entity

                url = data.get("@odata.nextLink")
                if url:
                    self.logger.debug("Following pagination link")

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error processing attachments for event {event_id}: {str(e)}")

    async def _process_single_attachment(  # noqa: C901
        self,
        attachment: Dict,
        event_id: str,
        breadcrumbs: List[Breadcrumb],
        att_idx: int,
        total_attachments: int,
        event_web_url: Optional[str],
        files: FileService | None = None,
    ) -> Optional[OutlookCalendarAttachmentEntity]:
        """Process a single attachment and return the processed entity."""
        attachment_id = attachment.get("id", "unknown")
        attachment_name = attachment.get("name", "unknown")

        self.logger.debug(
            f"Processing attachment #{att_idx + 1}/{total_attachments} "
            f"(ID: {attachment_id}, Name: {attachment_name})"
        )

        file_entity = OutlookCalendarAttachmentEntity.from_api(
            attachment,
            event_id=event_id,
            breadcrumbs=breadcrumbs,
            event_web_url=event_web_url,
        )
        if file_entity is None:
            self.logger.debug(f"Skipping non-file attachment: {attachment_name}")
            return None

        try:
            content_bytes = attachment.get("contentBytes")
            if not content_bytes:
                self.logger.debug(f"Fetching content for attachment {attachment_id}")
                attachment_url = (
                    f"{self.GRAPH_BASE_URL}/me/events/{event_id}/attachments/{attachment_id}"
                )
                attachment_data = await self._get(attachment_url)
                content_bytes = attachment_data.get("contentBytes")

                if not content_bytes:
                    self.logger.warning(f"No content found for attachment {attachment_name}")
                    return None

            try:
                binary_data = base64.b64decode(content_bytes)
            except Exception as e:
                self.logger.warning(f"Error decoding attachment content: {str(e)}")
                return None

            if files:
                try:
                    await files.save_bytes(
                        entity=file_entity,
                        content=binary_data,
                        filename_with_extension=attachment_name,
                        logger=self.logger,
                    )

                    if not file_entity.local_path:
                        raise ValueError(f"Save failed - no local path set for {file_entity.name}")

                    self.logger.debug(f"Successfully processed attachment: {attachment_name}")
                    return file_entity

                except FileSkippedException as e:
                    self.logger.debug(f"Skipping attachment {attachment_name}: {e.reason}")
                    return None

                except SourceAuthError:
                    raise

                except Exception as e:
                    self.logger.warning(f"Failed to save attachment {attachment_name}: {e}")
                    return None
            else:
                return file_entity

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error processing attachment {attachment_id}: {str(e)}")
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def generate_entities(
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate all Outlook Calendar entities: Calendars, Events and Attachments."""
        self.logger.info("Starting Outlook Calendar entity generation")
        entity_count = 0

        try:
            # One lookup for the whole run; every entity below carries the same owner.
            access = await self._owner_access()

            async for calendar_entity in self._generate_calendar_entities():
                entity_count += 1
                self.logger.info(
                    f"Yielding entity #{entity_count}: Calendar - {calendar_entity.name}"
                )
                calendar_entity.access = access
                yield calendar_entity

                async for event_entity in self._generate_event_entities(
                    calendar_entity, files=files
                ):
                    event_entity.access = access
                    entity_count += 1
                    entity_type = type(event_entity).__name__
                    entity_id = event_entity.entity_id
                    self.logger.info(
                        f"Yielding entity #{entity_count}: {entity_type} with ID {entity_id}"
                    )
                    yield event_entity

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error in entity generation: {str(e)}", exc_info=True)
            raise
        finally:
            self.logger.info(
                f"Outlook Calendar entity generation complete: {entity_count} entities"
            )

    async def validate(self) -> None:
        """Validate credentials by pinging the calendars endpoint."""
        await self._get(
            f"{self.GRAPH_BASE_URL}/me/calendars",
            params={"$top": "1"},
        )
