"""Microsoft OneNote source implementation.

Retrieves data from Microsoft OneNote, including:
 - User info (authenticated user)
 - Notebooks the user has access to
 - Section groups within notebooks
 - Sections within notebooks/section groups
 - Pages within sections

Reference:
  https://learn.microsoft.com/en-us/graph/api/resources/onenote
  https://learn.microsoft.com/en-us/graph/api/onenote-list-notebooks
  https://learn.microsoft.com/en-us/graph/api/notebook-list-sections
  https://learn.microsoft.com/en-us/graph/api/section-list-pages
"""

from typing import Any, AsyncGenerator, Dict, Optional

import httpx
from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.types import RateLimitLevel
from knowledge_index.connectors.runtime.types import NodeSelectionData
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.errors import FileSkippedException
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.configs import OneNoteConfig
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.acl import graph_owner_email, owner_to_access
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.onenote import (
    OneNoteNotebookEntity,
    OneNotePageFileEntity,
    OneNoteSectionEntity,
)
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.types import AuthenticationMethod, OAuthType


# Hosts the firm's Microsoft token may be sent to.
GRAPH_AUTH_HOSTS = ("graph.microsoft.com", "onenote.com")


@source(
    name="OneNote",
    short_name="onenote",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_ROTATING_REFRESH,
    auth_config_class=None,
    config_class=OneNoteConfig,
    labels=["Productivity", "Note Taking", "Collaboration"],
    supports_continuous=False,
    supports_access_control=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class OneNoteSource(BaseSource):
    """Microsoft OneNote source connector integrates with the Microsoft Graph API.

    Synchronizes data from Microsoft OneNote including notebooks, sections, and pages.

    It provides comprehensive access to OneNote resources with proper token refresh
    and rate limiting.

    The notebooks read through ``/me`` are an owner-scoped corpus: they belong to one
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
        config: OneNoteConfig,
    ) -> "OneNoteSource":
        """Create a new Microsoft OneNote source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        # Resolved once per run: the notebook owner does not change mid-sync, and a /me
        # call per page would spend quota to learn the same address repeatedly.
        instance._owner_email: Optional[str] = None
        instance._owner_resolved = False
        return instance

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    async def _owner_access(self) -> Optional[AccessControl]:
        """Who may read these notebooks: their owner, and nobody else.

        OneNote exposes no per-page permissions to a delegated token. What it does expose
        is whose notebooks these are, and a notebook belonging to one person is a real
        access control rather than the absence of one.

        Returns None when the owner cannot be resolved. None means "unknown" and keeps the
        pages fail-closed until an administrator grants access at the project level; an
        empty AccessControl would assert that nobody may read notebooks in active use.
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
                    f"Could not resolve the notebook owner ({e}); pages are indexed with "
                    "unknown access (fail-closed)."
                )
            self._owner_email = graph_owner_email(me)
            if me is not None and not self._owner_email:
                self.logger.warning(
                    "Microsoft Graph returned no address for the signed-in account; pages "
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
        """Make an authenticated GET request to Microsoft Graph API.

        Uses OAuth 2.0 with rotating refresh tokens.  On 401, attempts a
        single token refresh before letting ``raise_for_status`` translate
        the response into a ``SourceAuthError``.
        """
        headers = await self._authed_headers()
        response = await self.http_client.get(url, headers=headers, params=params)

        if response.status_code == 401 and self.auth.supports_refresh:
            self.logger.warning("Received 401 from Microsoft Graph — attempting token refresh")
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

    async def _generate_notebook_entities_with_sections(
        self,
    ) -> AsyncGenerator[tuple[OneNoteNotebookEntity, list], None]:
        """Generate OneNoteNotebookEntity objects with their sections data.

        Uses $expand to fetch sections in the same call, reducing API calls by ~22%.
        """
        self.logger.debug("Starting notebook entity generation with sections")
        url = f"{self.GRAPH_BASE_URL}/me/onenote/notebooks"
        params: Optional[dict] = {
            "$top": 100,
            "$expand": "sections",
            "$select": (
                "id,displayName,isDefault,isShared,userRole,createdDateTime,"
                "lastModifiedDateTime,createdBy,lastModifiedBy,links,self"
            ),
        }

        try:
            notebook_count = 0
            while url:
                self.logger.debug(f"Fetching notebooks from: {url}")
                data = await self._get(url, params=params)
                notebooks = data.get("value", [])
                self.logger.debug(f"Retrieved {len(notebooks)} notebooks with sections")

                for notebook_data in notebooks:
                    notebook_count += 1
                    display_name = notebook_data.get("displayName", "Unknown Notebook")

                    self.logger.debug(f"Processing notebook #{notebook_count}: {display_name}")

                    notebook_entity = OneNoteNotebookEntity.from_api(notebook_data)

                    sections_data = notebook_data.get("sections", [])
                    yield notebook_entity, sections_data

                url = data.get("@odata.nextLink")
                if url:
                    self.logger.debug("Following pagination to next page")
                    params = None

            self.logger.debug(f"Completed notebook generation. Total notebooks: {notebook_count}")

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating notebook entities: {str(e)}")
            raise

    async def _generate_section_entities(
        self,
        notebook_id: str,
        notebook_name: str,
        notebook_breadcrumb: Breadcrumb,
    ) -> AsyncGenerator[OneNoteSectionEntity, None]:
        """Generate OneNoteSectionEntity objects for sections in a notebook."""
        self.logger.debug(f"Starting section entity generation for notebook: {notebook_name}")
        url = f"{self.GRAPH_BASE_URL}/me/onenote/notebooks/{notebook_id}/sections"
        params: Optional[dict] = {"$top": 100}

        try:
            section_count = 0
            while url:
                self.logger.debug(f"Fetching sections from: {url}")
                data = await self._get(url, params=params)
                sections = data.get("value", [])
                self.logger.debug(
                    f"Retrieved {len(sections)} sections for notebook {notebook_name}"
                )

                for section_data in sections:
                    section_count += 1
                    display_name = section_data.get("displayName", "Unknown Section")

                    self.logger.debug(f"Processing section #{section_count}: {display_name}")

                    yield OneNoteSectionEntity.from_api(
                        section_data,
                        notebook_id=notebook_id,
                        notebook_breadcrumb=notebook_breadcrumb,
                    )

                url = data.get("@odata.nextLink")
                if url:
                    self.logger.debug("Following pagination to next page")
                    params = None

            self.logger.debug(
                f"Completed section generation for notebook {notebook_name}. "
                f"Total sections: {section_count}"
            )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Error generating section entities for notebook {notebook_name}: {str(e)}"
            )

    async def _generate_page_entities(  # noqa: C901
        self,
        section_id: str,
        section_name: str,
        notebook_id: str,
        section_breadcrumbs: list[Breadcrumb],
        files: FileService | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate processed OneNote page entities for pages in a section."""
        self.logger.debug(f"Starting page generation for section: {section_name}")
        url = f"{self.GRAPH_BASE_URL}/me/onenote/sections/{section_id}/pages"
        params: Optional[dict] = {
            "$top": 50,
            "$select": "id,title,contentUrl,level,order,createdDateTime,lastModifiedDateTime",
        }

        try:
            page_count = 0
            while url:
                self.logger.debug(f"Fetching pages from: {url}")
                data = await self._get(url, params=params)
                pages = data.get("value", [])
                self.logger.debug(f"Retrieved {len(pages)} pages for section {section_name}")

                for page_data in pages:
                    page_count += 1
                    title = page_data.get("title", "Untitled Page")
                    content_url = page_data.get("contentUrl")

                    if page_data.get("isDeleted") or page_data.get("deleted"):
                        self.logger.debug(f"Skipping deleted page: {title}")
                        continue

                    self.logger.debug(f"Processing page #{page_count}: {title}")

                    if not content_url:
                        self.logger.warning(f"Skipping page '{title}' - no content URL")
                        continue

                    if not title or title == "Untitled Page":
                        self.logger.debug(f"Skipping empty page '{title}'")
                        continue

                    self.logger.debug(f"Page '{title}': {content_url}")

                    file_entity = OneNotePageFileEntity.from_api(
                        page_data,
                        notebook_id=notebook_id,
                        section_id=section_id,
                        section_breadcrumbs=section_breadcrumbs,
                    )

                    if files:
                        try:
                            await files.download_from_url(
                                entity=file_entity,
                                client=self.http_client,
                                auth=self.auth,
                                logger=self.logger,
                                auth_hosts=GRAPH_AUTH_HOSTS,
                            )

                            if not file_entity.local_path:
                                self.logger.warning(
                                    f"Download produced no local path for {file_entity.name}"
                                )
                                continue

                            self.logger.debug(f"Successfully downloaded page: {file_entity.name}")
                            yield file_entity

                        except FileSkippedException as e:
                            self.logger.debug(f"Skipping page {title}: {e.reason}")
                            continue

                        except SourceAuthError:
                            raise

                        except httpx.HTTPStatusError as e:
                            if e.response.status_code == 401:
                                raise
                            self.logger.warning(
                                f"HTTP {e.response.status_code} downloading page {title}: {e}"
                            )
                            continue

                        except Exception as e:
                            self.logger.warning(f"Failed to download page {title}: {e}")
                            continue
                    else:
                        yield file_entity

                url = data.get("@odata.nextLink")
                if url:
                    self.logger.debug("Following pagination to next page")
                    params = None

            self.logger.debug(
                f"Completed page generation for section {section_name}. Total pages: {page_count}"
            )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating pages for section {section_name}: {str(e)}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def generate_entities(  # noqa: C901
        self,
        *,
        cursor: SyncCursor | None = None,
        files: FileService | None = None,
        node_selections: list[NodeSelectionData] | None = None,
    ) -> AsyncGenerator[BaseEntity, None]:
        """Generate all Microsoft OneNote entities.

        Yields entities in the following order:
          - OneNoteNotebookEntity for user's notebooks
          - OneNoteSectionEntity for sections in each notebook
          - OneNotePageFileEntity for pages in each section (processed as HTML files)
        """
        self.logger.debug("===== STARTING MICROSOFT ONENOTE ENTITY GENERATION =====")
        entity_count = 0

        try:
            self.logger.debug("Starting entity generation")

            # One lookup for the whole run; every entity below carries the same owner.
            access = await self._owner_access()

            async for (
                notebook_entity,
                sections_data,
            ) in self._generate_notebook_entities_with_sections():
                entity_count += 1
                self.logger.debug(
                    f"Yielding entity #{entity_count}: Notebook - {notebook_entity.display_name}"
                )
                notebook_entity.access = access
                yield notebook_entity

                notebook_id = notebook_entity.id
                notebook_breadcrumb = Breadcrumb(
                    entity_id=notebook_id,
                    name=notebook_entity.name,
                    entity_type="OneNoteNotebookEntity",
                )

                if sections_data:
                    self.logger.debug(
                        f"Processing {len(sections_data)} sections from expanded data (concurrent)"
                    )

                    def _create_section_worker(nb_breadcrumb, nb_id):
                        async def _section_worker(section_data):
                            section_id = section_data.get("id")
                            section_name = section_data.get("displayName", "Unknown Section")

                            section_entity = OneNoteSectionEntity.from_api(
                                section_data,
                                notebook_id=nb_id,
                                notebook_breadcrumb=nb_breadcrumb,
                            )

                            section_breadcrumb = Breadcrumb(
                                entity_id=section_id,
                                name=section_name,
                                entity_type="OneNoteSectionEntity",
                            )
                            section_breadcrumbs = [nb_breadcrumb, section_breadcrumb]

                            yield section_entity

                            async for page_entity in self._generate_page_entities(
                                section_id,
                                section_name,
                                nb_id,
                                section_breadcrumbs,
                                files=files,
                            ):
                                yield page_entity

                        return _section_worker

                    section_worker = _create_section_worker(notebook_breadcrumb, notebook_id)
                    async for entity in self.process_entities_concurrent(
                        items=sections_data,
                        worker=section_worker,
                        batch_size=getattr(self, "batch_size", 10),
                        preserve_order=False,
                        stop_on_error=False,
                        max_queue_size=getattr(self, "max_queue_size", 50),
                    ):
                        entity_count += 1
                        if hasattr(entity, "display_name"):
                            self.logger.debug(
                                f"Yielding entity #{entity_count}: Section - {entity.display_name}"
                            )
                        else:
                            self.logger.debug(
                                f"Yielding entity #{entity_count}: Page - {entity.title}"
                            )
                        entity.access = access
                        yield entity

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error in entity generation: {str(e)}", exc_info=True)
            raise
        finally:
            self.logger.debug(
                f"===== MICROSOFT ONENOTE ENTITY GENERATION COMPLETE: {entity_count} entities ====="
            )

    async def validate(self) -> None:
        """Validate credentials by pinging the OneNote notebooks endpoint."""
        await self._get(
            f"{self.GRAPH_BASE_URL}/me/onenote/notebooks",
            params={"$top": "1"},
        )
