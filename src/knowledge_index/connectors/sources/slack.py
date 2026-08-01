"""Slack source implementation.

Retrieves data from a Slack workspace through the Web API and produces indexable
documents:
 - public and private channels the app can see (containers)
 - messages in each channel, with thread replies folded into the message that
   started the thread

Two Slack-specific things shape this connector.

**The Web API signals failure inside a 200.** ``https://slack.com/api/<method>`` answers
``200 OK`` with ``{"ok": false, "error": "invalid_auth"}``. Checking the status code
alone would read a revoked token as an empty workspace, and the sync engine would
tombstone the firm's Slack corpus. Every response therefore goes through
:meth:`SlackSource._check_ok`, which routes the error string to the same typed
exceptions an HTTP status would have produced: rate limits to the retry path, dead
credentials to :class:`SourceAuthError`, and per-channel problems to a skip.

**Messages reference people by opaque id.** ``<@U012AB3CD>`` is unsearchable. One
``users.list`` sweep per run is cached and used twice: to render mentions and authors as
names, and to resolve private-channel membership to the ``user:{email}`` principals the
permission compiler matches callers against.

Scopes used, all of which are the ones declared for ``slack`` in ``providers.yaml``:
``channels:read``/``groups:read`` (conversations.list, conversations.members),
``channels:history``/``groups:history`` (conversations.history, conversations.replies),
``users:read``/``users:read.email`` (users.list). ``auth.test`` needs no scope.

Reference:
  https://api.slack.com/methods/conversations.list
  https://api.slack.com/methods/conversations.history
  https://api.slack.com/methods/conversations.replies
  https://api.slack.com/methods/conversations.members
  https://api.slack.com/methods/users.list
  https://api.slack.com/methods/auth.test
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List, Optional

from tenacity import retry, stop_after_attempt

from knowledge_index.connectors.acl import slack_channel_to_access
from knowledge_index.connectors.base import BaseSource
from knowledge_index.connectors.configs import SlackConfig
from knowledge_index.connectors.cursors.state import SyncCursor
from knowledge_index.connectors.decorators import source
from knowledge_index.connectors.entities._base import AccessControl, BaseEntity
from knowledge_index.connectors.entities.slack import (
    NOISE_SUBTYPES,
    SlackChannelEntity,
    SlackMessageEntity,
)
from knowledge_index.connectors.http_helpers import raise_for_status
from knowledge_index.connectors.retry import (
    retry_if_rate_limit_or_timeout,
    wait_rate_limit_with_backoff,
)
from knowledge_index.connectors.runtime.errors import (
    SourceAuthError,
    SourceEntityError,
    SourceEntitySkippedError,
    SourceError,
    SourceRateLimitError,
)
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.runtime.http import HttpClient
from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.tokens import TokenProviderProtocol
from knowledge_index.connectors.runtime.types import (
    AuthenticationMethod,
    MembershipTuple,
    NodeSelectionData,
    OAuthType,
    RateLimitLevel,
)

# Slack asked us to slow down. Retryable.
RATE_LIMIT_ERRORS = frozenset({"ratelimited", "rate_limited"})

# The credentials are dead and no retry will fix them. Must abort the sync rather than
# report an empty workspace — see the module docstring.
AUTH_ERRORS = frozenset(
    {
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "not_authed",
        "invalid_token",
    }
)

# One channel is unreadable or gone. The rest of the workspace is unaffected, so this is
# a logged skip: aborting here would lose every channel after the first archived one.
CHANNEL_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "restricted_action",
        "thread_not_found",
        "message_not_found",
        "user_is_restricted",
        "fetch_members_failed",
    }
)

API_BASE = "https://slack.com/api"

# Slack's documented maximum for these cursor-paginated methods; it advises against
# asking for more than 200 on conversations.history.
PAGE_SIZE = 200


@source(
    name="Slack",
    short_name="slack",
    auth_methods=[
        AuthenticationMethod.OAUTH_BROWSER,
        AuthenticationMethod.OAUTH_TOKEN,
    ],
    oauth_type=OAuthType.ACCESS_ONLY,
    auth_config_class=None,
    config_class=SlackConfig,
    labels=["Messaging"],
    supports_continuous=False,
    supports_access_control=True,
    rate_limit_level=RateLimitLevel.ORG,
)
class SlackSource(BaseSource):
    """Slack source connector integrating with the Slack Web API.

    Indexes channel messages as documents rather than answering queries live, so the
    corpus is searchable alongside a firm's documents and subject to the same permission
    compilation.
    """

    @classmethod
    async def create(
        cls,
        *,
        auth: TokenProviderProtocol,
        logger: ContextualLogger,
        http_client: HttpClient,
        config: SlackConfig,
    ) -> SlackSource:
        """Create a new Slack source instance."""
        instance = cls(auth=auth, logger=logger, http_client=http_client)
        instance._config = config or SlackConfig()
        # Resolved once per run and reused: a users.list sweep per channel would burn the
        # workspace's Tier 2 quota on data that does not change during a sync.
        instance._user_names: Dict[str, str] = {}
        instance._user_emails: Optional[Dict[str, str]] = None
        instance._users_loaded = False
        instance._workspace_url: Optional[str] = None
        instance._workspace_url_loaded = False
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

    @retry(
        stop=stop_after_attempt(5),
        retry=retry_if_rate_limit_or_timeout,
        wait=wait_rate_limit_with_backoff,
        reraise=True,
    )
    async def _call(self, method: str, params: Optional[dict] = None) -> Dict[str, Any]:
        """Call one Web API method and return its payload.

        Slack tokens for this connector are ``access_only`` — there is no refresh to
        attempt on a 401, so a dead credential is reported as such immediately.
        """
        headers = await self._authed_headers()
        response = await self.http_client.get(
            f"{API_BASE}/{method}", headers=headers, params=params
        )
        raise_for_status(
            response,
            source_short_name=self.short_name,
            token_provider_kind=self.auth.provider_kind,
            context=f"calling {method}",
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise SourceError(
                f"Slack {method} returned {type(payload).__name__}, not an object",
                source_short_name=self.short_name,
            )
        self._check_ok(payload, method, retry_after=response.headers.get("Retry-After"))
        return payload

    def _check_ok(
        self, payload: Dict[str, Any], method: str, *, retry_after: Optional[str] = None
    ) -> None:
        """Translate ``{"ok": false}`` into the same exceptions a bad status would give.

        This is the one place the 200-with-an-error quirk is handled. Every caller relies
        on it, so no caller has to remember the quirk.
        """
        if payload.get("ok"):
            return
        error = str(payload.get("error") or "unknown_error")
        detail = f"Slack {method} failed: {error}"

        if error in RATE_LIMIT_ERRORS:
            seconds = 30.0
            try:
                seconds = max(float(retry_after), 1.0) if retry_after else seconds
            except (TypeError, ValueError):
                pass
            raise SourceRateLimitError(
                f"{detail}. Retry after {seconds:.1f}s",
                retry_after=seconds,
                source_short_name=self.short_name,
            )
        if error in AUTH_ERRORS:
            raise SourceAuthError(
                f"{detail} — credentials invalid or revoked. Re-authorize the connection.",
                source_short_name=self.short_name,
                token_provider_kind=self.auth.provider_kind,
            )
        if error in CHANNEL_ERRORS:
            raise SourceEntitySkippedError(detail, source_short_name=self.short_name)
        if error == "missing_scope":
            raise SourceError(
                f"{detail} — the Slack app is missing a scope it needs "
                f"(wanted {payload.get('needed')!r}, has {payload.get('provided')!r}). "
                "Re-authorize with the scopes declared for slack in providers.yaml.",
                source_short_name=self.short_name,
            )
        raise SourceError(detail, source_short_name=self.short_name)

    async def _paginate(
        self, method: str, params: dict, key: str
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Walk a cursor-paginated Web API method, yielding each item under ``key``."""
        cursor = ""
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = await self._call(method, page_params)
            for item in payload.get(key) or []:
                if isinstance(item, dict):
                    yield item
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                return

    # ------------------------------------------------------------------
    # Per-run caches
    # ------------------------------------------------------------------

    async def _ensure_users(self) -> None:
        """Load the workspace user directory once, for names and for ACL resolution.

        A failure here is not fatal: mentions stay as ids and private channels are left
        with unknown access, which is fail-closed. Aborting the whole sync because a
        directory read failed would be worse than indexing public channels only.
        """
        if self._users_loaded:
            return
        self._users_loaded = True
        names: Dict[str, str] = {}
        emails: Dict[str, str] = {}
        try:
            async for user in self._paginate("users.list", {"limit": PAGE_SIZE}, "members"):
                user_id = str(user.get("id") or "").strip()
                if not user_id:
                    continue
                profile = user.get("profile") or {}
                display = (
                    str(profile.get("display_name") or "").strip()
                    or str(profile.get("real_name") or "").strip()
                    or str(user.get("real_name") or "").strip()
                    or str(user.get("name") or "").strip()
                )
                if display:
                    names[user_id] = display
                email = str(profile.get("email") or "").strip()
                if email:
                    emails[user_id] = email
            self._user_names = names
            self._user_emails = emails
            self.logger.info(
                f"Cached Slack directory: {len(names)} display names, {len(emails)} emails"
            )
        except SourceAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - see docstring
            self.logger.warning(
                f"Could not read the Slack user directory ({e}). Mentions will keep their "
                "raw ids and private-channel access will be left unknown."
            )
            self._user_names = {}
            self._user_emails = None

    async def _ensure_workspace_url(self) -> None:
        """Learn the workspace's archive host so permalinks can be built offline.

        ``auth.test`` returns it and needs no scope, which is cheaper and less
        rate-limited than a ``chat.getPermalink`` per message.
        """
        if self._workspace_url_loaded:
            return
        self._workspace_url_loaded = True
        try:
            payload = await self._call("auth.test")
            self._workspace_url = str(payload.get("url") or "").strip() or None
        except SourceAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - permalinks are a convenience, not content
            self.logger.warning(
                f"Could not resolve the Slack workspace URL ({e}); messages will be "
                "indexed without permalinks"
            )
            self._workspace_url = None

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    async def _channel_access(self, channel: Dict[str, Any]) -> Optional[AccessControl]:
        """Decide who may read a channel's messages.

        Public channels are workspace-readable. Private channels are resolved from their
        membership; if that cannot be read the result is ``None``, never an empty
        ``AccessControl`` — see :func:`slack_channel_to_access`.
        """
        if not channel.get("is_private"):
            return slack_channel_to_access(is_private=False)

        channel_id = str(channel.get("id") or "")
        await self._ensure_users()
        member_ids = await self._channel_member_ids(channel_id)
        access = slack_channel_to_access(
            is_private=True,
            member_ids=member_ids,
            emails_by_user_id=self._user_emails,
        )
        if access is None:
            self.logger.warning(
                f"Membership of private channel {channel_id} could not be resolved to "
                "emails; its messages are indexed with unknown access (fail-closed) "
                "until an administrator grants access at the project level."
            )
        return access

    async def _channel_member_ids(self, channel_id: str) -> Optional[List[str]]:
        """List a channel's member ids, or ``None`` if the membership cannot be read.

        Paginated by hand rather than through :meth:`_paginate` because
        ``conversations.members`` returns bare id strings, not objects.
        """
        members: List[str] = []
        cursor = ""
        try:
            while True:
                params: Dict[str, Any] = {"channel": channel_id, "limit": PAGE_SIZE}
                if cursor:
                    params["cursor"] = cursor
                payload = await self._call("conversations.members", params)
                members.extend(str(item) for item in (payload.get("members") or []) if item)
                cursor = str(
                    (payload.get("response_metadata") or {}).get("next_cursor") or ""
                ).strip()
                if not cursor:
                    break
        except SourceAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - unknown membership is fail-closed, not fatal
            self.logger.warning(f"Could not read members of channel {channel_id}: {e}")
            return None
        return members

    async def generate_access_control_memberships(
        self,
    ) -> AsyncGenerator[MembershipTuple, None]:
        """Slack channel ACLs are already expanded, so there are no groups to mirror.

        Private-channel access is emitted as ``user:{email}`` principals rather than as a
        group id, because Slack's channel membership *is* the flat member list. There is
        nothing left for the permission compiler to expand, so this yields nothing rather
        than inventing a synthetic group per channel.
        """
        return
        yield  # pragma: no cover

    # ------------------------------------------------------------------
    # Entity generators
    # ------------------------------------------------------------------

    async def _generate_channel_entities(self) -> AsyncGenerator[SlackChannelEntity, None]:
        """Generate SlackChannelEntity objects for the channels the app can see."""
        types = ["public_channel"]
        if self._config.include_private_channels:
            types.append("private_channel")
        self.logger.info(f"Starting Slack channel generation (types: {', '.join(types)})")

        await self._ensure_workspace_url()
        params = {
            "types": ",".join(types),
            "limit": PAGE_SIZE,
            "exclude_archived": "true",
        }

        channel_count = 0
        async for channel in self._paginate("conversations.list", params, "channels"):
            if not channel.get("id"):
                continue
            if channel.get("is_archived"):
                # Belt and braces: exclude_archived is a request, and a channel archived
                # mid-crawl still arrives.
                self.logger.debug(f"Skipping archived channel {channel.get('name')}")
                continue
            channel_count += 1
            yield SlackChannelEntity.from_api(channel, workspace_url=self._workspace_url)

        self.logger.info(f"Completed Slack channel generation. Total channels: {channel_count}")

    async def _generate_message_entities(
        self,
        channel: SlackChannelEntity,
        access: Optional[AccessControl],
    ) -> AsyncGenerator[SlackMessageEntity, None]:
        """Generate SlackMessageEntity objects for one channel's history."""
        channel_id = channel.id
        channel_name = channel.channel_name
        self.logger.info(f"Starting message generation for channel #{channel_name}")

        await self._ensure_users()
        breadcrumbs = [channel.breadcrumb()]
        cap = max(int(self._config.max_messages_per_channel or 0), 0)
        message_count = 0
        cursor = ""

        while True:
            page_limit = PAGE_SIZE if not cap else min(PAGE_SIZE, cap - message_count)
            if page_limit <= 0:
                break
            params: Dict[str, Any] = {"channel": channel_id, "limit": page_limit}
            if cursor:
                params["cursor"] = cursor
            payload = await self._call("conversations.history", params)
            messages = payload.get("messages") or []
            self.logger.debug(f"Retrieved {len(messages)} messages for #{channel_name}")

            for message in messages:
                if not isinstance(message, dict) or not message.get("ts"):
                    continue
                if str(message.get("subtype") or "") in NOISE_SUBTYPES:
                    continue
                replies = await self._thread_replies(channel_id, message)
                yield SlackMessageEntity.from_api(
                    message,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    breadcrumbs=breadcrumbs,
                    user_names=self._user_names,
                    replies=replies,
                    permalink=_permalink(self._workspace_url, channel_id, str(message["ts"])),
                    access=access,
                )
                message_count += 1
                if cap and message_count >= cap:
                    self.logger.info(
                        f"Reached the configured cap of {cap} messages for #{channel_name}"
                    )
                    return

            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break

        self.logger.info(
            f"Completed message generation for #{channel_name}. Total messages: {message_count}"
        )

    async def _thread_replies(
        self, channel_id: str, message: Dict[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch a thread's replies, or ``None`` when there is no thread to fetch.

        A thread's replies are indexed with the message that started it rather than as
        separate documents: a reply reading "agreed" retrieves nothing useful on its own,
        and Slack's channel history does not return replies anyway.
        """
        if not self._config.include_thread_replies:
            return None
        ts = str(message.get("ts") or "")
        thread_ts = str(message.get("thread_ts") or "")
        if not thread_ts or thread_ts != ts:
            return None
        if not int(message.get("reply_count") or 0):
            return None
        try:
            replies = [
                reply
                async for reply in self._paginate(
                    "conversations.replies",
                    {"channel": channel_id, "ts": ts, "limit": PAGE_SIZE},
                    "messages",
                )
            ]
        except SourceAuthError:
            raise
        except Exception as e:  # noqa: BLE001 - the parent message is still worth indexing
            self.logger.warning(f"Could not read thread {ts} in channel {channel_id}: {e}")
            return None
        return replies or None

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
        """Generate all Slack entities.

        Yields, per channel:
          - SlackChannelEntity (a container, carrying the channel's access decision)
          - SlackMessageEntity for each message in the channel's history

        One unreadable channel — archived mid-crawl, or one the app was removed from — is
        logged and skipped. Only dead credentials abort the run.
        """
        self.logger.info("Starting Slack entity generation")
        entity_count = 0

        async for channel_entity in self._generate_channel_entities():
            try:
                access = await self._channel_access(
                    {"id": channel_entity.id, "is_private": channel_entity.is_private}
                )
            except SourceAuthError:
                raise
            except Exception as e:  # noqa: BLE001 - unknown access is fail-closed
                self.logger.warning(
                    f"Could not determine access for #{channel_entity.channel_name}: {e}"
                )
                access = None

            channel_entity.access = access
            entity_count += 1
            yield channel_entity

            try:
                async for message_entity in self._generate_message_entities(
                    channel_entity, access
                ):
                    entity_count += 1
                    yield message_entity
            except SourceAuthError:
                raise
            except SourceEntityError as e:
                self.logger.warning(
                    f"Skipping channel #{channel_entity.channel_name}: {e}"
                )
            except Exception as e:  # noqa: BLE001 - one channel must not lose the rest
                self.logger.warning(
                    f"Error generating messages for #{channel_entity.channel_name}: {e}"
                )

        self.logger.info(f"Slack entity generation complete: {entity_count} entities")

    async def validate(self) -> None:
        """Validate credentials by calling auth.test, which needs no scope."""
        await self._call("auth.test")


def _permalink(workspace_url: Optional[str], channel_id: str, ts: str) -> Optional[str]:
    """Build a Slack archive link without spending a ``chat.getPermalink`` call."""
    if not workspace_url or not ts:
        return None
    return f"{workspace_url.rstrip('/')}/archives/{channel_id}/p{ts.replace('.', '')}"
