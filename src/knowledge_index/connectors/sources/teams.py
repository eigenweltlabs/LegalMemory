"""Microsoft Teams source implementation.

Retrieves data from Microsoft Teams, including:
 - Teams the user has joined
 - Channels within teams
 - Chats (1:1, group, meeting)
 - Messages in channels and chats
 - Team members

Reference:
  https://learn.microsoft.com/en-us/graph/api/resources/teams-api-overview
  https://learn.microsoft.com/en-us/graph/api/user-list-joinedteams
  https://learn.microsoft.com/en-us/graph/api/channel-list
  https://learn.microsoft.com/en-us/graph/api/chat-list
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional

from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.acl import teams_members_to_access
from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.types import MembershipTuple, RateLimitLevel
from knowledge_index.connectors.runtime.types import NodeSelectionData
from knowledge_index.connectors.runtime.errors import SourceAuthError
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.configs import TeamsConfig
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity, Breadcrumb
from knowledge_index.connectors.entities.teams import (
    TeamsChannelEntity,
    TeamsChatEntity,
    TeamsMessageEntity,
    TeamsTeamEntity,
    _parse_dt,
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
    name="Teams",
    short_name="teams",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
        AuthenticationMethod.AUTH_PROVIDER,
    ],
    oauth_type=OAuthType.WITH_ROTATING_REFRESH,
    auth_config_class=None,
    config_class=TeamsConfig,
    labels=["Communication", "Collaboration"],
    supports_continuous=False,
    supports_access_control=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class TeamsSource(BaseSource):
    """Microsoft Teams source connector integrates with the Microsoft Graph API.

    Synchronizes data from Microsoft Teams including teams, channels, chats, and messages.

    It provides comprehensive access to Teams resources with proper token refresh
    and rate limiting.

    Messages are indexed with the audience of the conversation they were posted in: a
    standard channel's messages carry the team's backing Entra group, a private channel's
    and a chat's carry their own members. Where that cannot be read the messages keep
    unknown access, which is fail-closed.
    """

    GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: TeamsConfig,
    ) -> TeamsSource:
        """Create a new Microsoft Teams source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._mirror_permissions = bool(config.mirror_permissions) if config else True
        # A team's backing group is the same for every channel in it, so it is resolved
        # once per team rather than once per channel. None is cached too: a team whose
        # group could not be read must not be retried for each of its channels.
        instance._team_group_ids: Dict[str, Optional[str]] = {}
        instance._team_names: Dict[str, str] = {}
        return instance

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
    async def _get(self, url: str, params: dict | None = None) -> Any:
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
    # Access control
    # ------------------------------------------------------------------

    async def _team_group_id(self, team_id: str) -> Optional[str]:
        """Resolve the Entra group backing a team, cached for the run.

        A team is a group: ``GET /teams/{id}`` answers with the same id the group is known
        by in the directory, which is what mirrored memberships expand. Reading it back
        rather than assuming the identity keeps a team the token cannot see from being
        turned into a group grant that does not exist.

        Returns None when the team could not be read. None means "unknown" and leaves the
        channel's messages fail-closed; it never aborts the scan.
        """
        if team_id in self._team_group_ids:
            return self._team_group_ids[team_id]
        group_id: Optional[str] = None
        try:
            data = await self._get(f"{self.GRAPH_BASE_URL}/teams/{team_id}")
            group_id = str(data.get("id") or "").strip() or None
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Could not read team {team_id} to resolve its group: {e}. Its standard "
                "channels are indexed with unknown access (fail-closed)."
            )
        self._team_group_ids[team_id] = group_id
        return group_id

    async def _list_members(self, url: str) -> Optional[List[Dict[str, Any]]]:
        """Page through a Graph members collection, or None if it cannot be read."""
        members: List[Dict[str, Any]] = []
        next_url: str | None = url
        try:
            while next_url:
                data = await self._get(next_url)
                members.extend(data.get("value", []) or [])
                next_url = data.get("@odata.nextLink")
        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Could not read members from {url}: {e}")
            return None
        return members

    async def _channel_access(
        self, team_id: str, channel_id: str, membership_type: str | None
    ) -> Optional[AccessControl]:
        """Decide who may read a channel's messages.

        A standard channel is readable by the whole team, so the team's backing group is
        emitted and expanded through mirrored memberships — enumerating the team here
        would go stale the moment somebody joins. A private channel carries its own
        membership and is resolved to the members' own principals.

        Returns None when the lookup failed. None means "unknown" and keeps the messages
        fail-closed; an empty AccessControl would assert that a channel the firm is
        actively using may be read by nobody.
        """
        if not self._mirror_permissions:
            return None
        if str(membership_type or "").strip().casefold() == "private":
            members = await self._list_members(
                f"{self.GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/members"
            )
            if members is None:
                self.logger.warning(
                    f"Membership of private channel {channel_id} could not be read; its "
                    "messages are indexed with unknown access (fail-closed)."
                )
                return None
            return self._resolved_or_unknown(
                teams_members_to_access(members), f"private channel {channel_id}"
            )

        group_id = await self._team_group_id(team_id)
        if not group_id:
            return None
        return teams_members_to_access(None, team_group_id=group_id)

    async def _chat_access(self, chat_id: str) -> Optional[AccessControl]:
        """Decide who may read a chat's messages.

        A chat is readable by its participants and nobody else, so it is mirrored from its
        own member list. Unreadable membership leaves the messages fail-closed rather than
        publishing a private conversation to the firm.
        """
        if not self._mirror_permissions:
            return None
        members = await self._list_members(f"{self.GRAPH_BASE_URL}/chats/{chat_id}/members")
        if members is None:
            self.logger.warning(
                f"Membership of chat {chat_id} could not be read; its messages are "
                "indexed with unknown access (fail-closed)."
            )
            return None
        return self._resolved_or_unknown(teams_members_to_access(members), f"chat {chat_id}")

    def _resolved_or_unknown(
        self, access: Optional[AccessControl], subject: str
    ) -> Optional[AccessControl]:
        """Downgrade a membership that resolved to no principal at all to "unknown".

        Graph can answer with members this appliance cannot name — a federated guest with
        no readable email, say. Keeping the resulting empty viewer list would assert that
        an actively used conversation may be read by nobody, which is a claim, not a gap.
        None says "unknown" and is reported as such.
        """
        if access is None or access.viewers or access.is_public:
            return access
        self.logger.warning(
            f"Membership of {subject} resolved to no usable principal; its messages are "
            "indexed with unknown access (fail-closed)."
        )
        return None

    # ------------------------------------------------------------------
    # Entity generators
    # ------------------------------------------------------------------

    async def _generate_team_entities(self) -> AsyncGenerator[TeamsTeamEntity, None]:
        """Generate TeamsTeamEntity objects for teams the user has joined."""
        self.logger.info("Starting team entity generation")
        url: str | None = f"{self.GRAPH_BASE_URL}/me/joinedTeams"

        try:
            team_count = 0
            while url:
                data = await self._get(url)
                teams = data.get("value", [])
                self.logger.info(f"Retrieved {len(teams)} teams")

                for team_data in teams:
                    team_count += 1
                    team_id = team_data.get("id")
                    display_name = team_data.get("displayName", "Unknown Team")
                    if team_id:
                        self._team_names[str(team_id)] = str(display_name)

                    yield TeamsTeamEntity(
                        breadcrumbs=[],
                        id=team_id,
                        name=display_name,
                        created_at=_parse_dt(team_data.get("createdDateTime")),
                        updated_at=None,
                        display_name=display_name,
                        description=team_data.get("description"),
                        visibility=team_data.get("visibility"),
                        is_archived=team_data.get("isArchived"),
                        web_url=team_data.get("webUrl"),
                        web_url_override=team_data.get("webUrl"),
                        classification=team_data.get("classification"),
                        specialization=team_data.get("specialization"),
                        internal_id=team_data.get("internalId"),
                    )

                url = data.get("@odata.nextLink")

            self.logger.info(f"Completed team generation. Total teams: {team_count}")

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating team entities: {e}")
            raise

    async def _generate_channel_entities(
        self, team_id: str, team_name: str
    ) -> AsyncGenerator[TeamsChannelEntity, None]:
        """Generate TeamsChannelEntity objects for channels in a team."""
        self.logger.info(f"Starting channel entity generation for team: {team_name}")
        url: str | None = f"{self.GRAPH_BASE_URL}/teams/{team_id}/channels"

        try:
            channel_count = 0
            while url:
                data = await self._get(url)
                channels = data.get("value", [])
                self.logger.info(f"Retrieved {len(channels)} channels for team {team_name}")

                for channel_data in channels:
                    channel_count += 1
                    channel_id = channel_data.get("id")
                    display_name = channel_data.get("displayName", "Unknown Channel")

                    yield TeamsChannelEntity(
                        breadcrumbs=[
                            Breadcrumb(
                                entity_id=team_id,
                                name=team_name,
                                entity_type="TeamsTeamEntity",
                            )
                        ],
                        id=channel_id,
                        name=display_name,
                        created_at=_parse_dt(channel_data.get("createdDateTime")),
                        updated_at=None,
                        team_id=team_id,
                        display_name=display_name,
                        description=channel_data.get("description"),
                        email=channel_data.get("email"),
                        membership_type=channel_data.get("membershipType"),
                        is_archived=channel_data.get("isArchived"),
                        is_favorite_by_default=channel_data.get("isFavoriteByDefault"),
                        web_url_override=channel_data.get("webUrl"),
                    )

                url = data.get("@odata.nextLink")

            self.logger.info(
                f"Completed channel generation for team {team_name}. "
                f"Total channels: {channel_count}"
            )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating channel entities for team {team_name}: {e}")

    async def _generate_channel_message_entities(
        self,
        team_id: str,
        team_name: str,
        channel_id: str,
        channel_name: str,
        team_breadcrumb: Breadcrumb,
        channel_breadcrumb: Breadcrumb,
        access: Optional[AccessControl] = None,
    ) -> AsyncGenerator[TeamsMessageEntity, None]:
        """Generate TeamsMessageEntity objects for messages in a channel.

        Every message carries the channel's access decision: a message is visible to
        whoever can see the channel it was posted in.
        """
        self.logger.info(f"Starting message generation for channel: {channel_name}")
        url: str | None = f"{self.GRAPH_BASE_URL}/teams/{team_id}/channels/{channel_id}/messages"
        params: dict | None = {"$top": 50}

        try:
            message_count = 0
            while url:
                data = await self._get(url, params=params)
                messages = data.get("value", [])
                self.logger.info(f"Retrieved {len(messages)} messages for channel {channel_name}")

                for message_data in messages:
                    message_count += 1
                    message_entity = TeamsMessageEntity.from_api(
                        message_data,
                        breadcrumbs=[team_breadcrumb, channel_breadcrumb],
                        team_id=team_id,
                        channel_id=channel_id,
                    )
                    message_entity.access = access
                    yield message_entity

                url = data.get("@odata.nextLink")
                if url:
                    params = None

            self.logger.info(
                f"Completed message generation for channel {channel_name}. "
                f"Total messages: {message_count}"
            )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating messages for channel {channel_name}: {e}")

    async def _generate_chat_entities(self) -> AsyncGenerator[TeamsChatEntity, None]:
        """Generate TeamsChatEntity objects for user's chats."""
        self.logger.info("Starting chat entity generation")
        url: str | None = f"{self.GRAPH_BASE_URL}/me/chats"
        params: dict | None = {"$top": 50}

        try:
            chat_count = 0
            while url:
                data = await self._get(url, params=params)
                chats = data.get("value", [])
                self.logger.info(f"Retrieved {len(chats)} chats")

                for chat_data in chats:
                    chat_count += 1
                    chat_id = chat_data.get("id")
                    topic = chat_data.get("topic", "")
                    chat_type = chat_data.get("chatType", "oneOnOne")
                    name = topic if topic else f"{chat_type} chat"

                    yield TeamsChatEntity(
                        breadcrumbs=[],
                        id=chat_id,
                        name=name,
                        created_at=_parse_dt(chat_data.get("createdDateTime")),
                        updated_at=_parse_dt(chat_data.get("lastUpdatedDateTime")),
                        chat_type=chat_type,
                        topic_label=name,
                        topic=topic if topic else None,
                        web_url_override=chat_data.get("webUrl"),
                    )

                url = data.get("@odata.nextLink")
                if url:
                    params = None

            self.logger.info(f"Completed chat generation. Total chats: {chat_count}")

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating chat entities: {e}")

    async def _generate_chat_message_entities(
        self,
        chat_id: str,
        chat_topic: str | None,
        chat_breadcrumb: Breadcrumb,
        access: Optional[AccessControl] = None,
    ) -> AsyncGenerator[TeamsMessageEntity, None]:
        """Generate TeamsMessageEntity objects for messages in a chat.

        Every message carries the chat's access decision: a message is visible to the
        chat's participants.
        """
        display_chat = chat_topic if chat_topic else chat_id[:8]
        self.logger.info(f"Starting message generation for chat: {display_chat}")
        url: str | None = f"{self.GRAPH_BASE_URL}/chats/{chat_id}/messages"
        params: dict | None = {"$top": 50}

        try:
            message_count = 0
            while url:
                data = await self._get(url, params=params)
                messages = data.get("value", [])
                self.logger.info(f"Retrieved {len(messages)} messages for chat {display_chat}")

                for message_data in messages:
                    message_count += 1
                    message_entity = TeamsMessageEntity.from_api(
                        message_data,
                        breadcrumbs=[chat_breadcrumb],
                        chat_id=chat_id,
                    )
                    message_entity.access = access
                    yield message_entity

                url = data.get("@odata.nextLink")
                if url:
                    params = None

            self.logger.info(
                f"Completed message generation for chat {display_chat}. "
                f"Total messages: {message_count}"
            )

        except SourceAuthError:
            raise
        except Exception as e:
            self.logger.warning(f"Error generating messages for chat {display_chat}: {e}")

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
        """Generate all Microsoft Teams entities.

        Yields entities in the following order:
          - TeamsTeamEntity for teams the user has joined
          - TeamsChannelEntity for channels in each team
          - TeamsMessageEntity for messages in each channel
          - TeamsChatEntity for user's chats
          - TeamsMessageEntity for messages in each chat
        """
        self.logger.info("Starting Microsoft Teams entity generation")
        entity_count = 0

        async for team_entity in self._generate_team_entities():
            entity_count += 1
            yield team_entity

            team_id = team_entity.id
            team_name = team_entity.display_name
            team_breadcrumb = Breadcrumb(
                entity_id=team_id,
                name=team_entity.display_name,
                entity_type="TeamsTeamEntity",
            )

            async for channel_entity in self._generate_channel_entities(team_id, team_name):
                channel_id = channel_entity.id
                channel_name = channel_entity.display_name

                try:
                    access = await self._channel_access(
                        team_id, channel_id, channel_entity.membership_type
                    )
                except SourceAuthError:
                    raise
                except Exception as e:  # noqa: BLE001 - unknown access is fail-closed
                    self.logger.warning(
                        f"Could not determine access for channel {channel_name}: {e}"
                    )
                    access = None

                channel_entity.access = access
                entity_count += 1
                yield channel_entity

                channel_breadcrumb = Breadcrumb(
                    entity_id=channel_id,
                    name=channel_entity.display_name,
                    entity_type="TeamsChannelEntity",
                )

                async for message_entity in self._generate_channel_message_entities(
                    team_id,
                    team_name,
                    channel_id,
                    channel_name,
                    team_breadcrumb,
                    channel_breadcrumb,
                    access=access,
                ):
                    entity_count += 1
                    yield message_entity

        async for chat_entity in self._generate_chat_entities():
            chat_id = chat_entity.id

            try:
                access = await self._chat_access(chat_id)
            except SourceAuthError:
                raise
            except Exception as e:  # noqa: BLE001 - unknown access is fail-closed
                self.logger.warning(f"Could not determine access for chat {chat_id}: {e}")
                access = None

            chat_entity.access = access
            entity_count += 1
            yield chat_entity

            chat_breadcrumb = Breadcrumb(
                entity_id=chat_id,
                name=chat_entity.name,
                entity_type="TeamsChatEntity",
            )

            async for message_entity in self._generate_chat_message_entities(
                chat_id, chat_entity.topic, chat_breadcrumb, access=access
            ):
                entity_count += 1
                yield message_entity

        self.logger.info(f"Microsoft Teams entity generation complete: {entity_count} entities")

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Expand the Entra groups carried by standard-channel ACLs.

        A caller signs in as a person, not as an opaque team id. Only groups actually
        emitted during this scan are expanded; private channels and chats already carry
        their members directly.

        An unreadable collection yields no edges. The sync engine replaces the previous
        snapshot with that empty result, deliberately failing closed rather than
        preserving access the directory no longer proves.
        """
        if not self._mirror_permissions:
            return

        for team_id, group_id in sorted(self._team_group_ids.items()):
            if not group_id:
                continue
            members = await self._list_members(
                f"{self.GRAPH_BASE_URL}/teams/{team_id}/members"
            )
            if members is None:
                continue
            for member in members:
                email = str(
                    member.get("email")
                    or member.get("mail")
                    or member.get("userPrincipalName")
                    or ""
                ).strip()
                if not email:
                    continue
                yield MembershipTuple(
                    member_id=email,
                    member_type="user",
                    group_id=f"entra:{group_id}",
                    group_name=self._team_names.get(team_id) or team_id,
                )

    async def validate(self) -> None:
        """Validate credentials by pinging the joinedTeams endpoint."""
        await self._get(f"{self.GRAPH_BASE_URL}/me/joinedTeams")
