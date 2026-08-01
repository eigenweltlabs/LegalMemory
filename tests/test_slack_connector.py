"""The Slack connector driven against recorded Web API payloads.

Slack is the one connector here that indexes rather than proxies: it walks a workspace's
conversations and produces documents. That makes three things worth pinning down, because
each of them fails silently rather than loudly:

* **The 200-with-an-error quirk.** ``https://slack.com/api/...`` answers ``200 OK`` with
  ``{"ok": false, "error": "invalid_auth"}``. A connector that trusts the status code
  reads a revoked token as an empty workspace, and the engine's tombstone path then
  deletes the firm's Slack corpus. The auth error must reach the caller as one.
* **Entity ids.** A Slack message has no workspace-wide id; ``ts`` repeats across
  channels. The fixtures below deliberately post the *same* ``ts`` in two channels, so a
  regression to a bare ``ts`` key shows up as one message overwriting another.
* **Channel privacy.** A public channel is readable by the whole workspace; a private one
  by its members. Getting the second wrong publishes a matter-specific channel to the
  firm, so unresolvable membership stays unknown (fail-closed) rather than empty.

The client is a fake that answers Slack methods from a recorded workspace, dispatching on
the method name and its parameters, so per-channel behaviour — one broken channel, cursor
pagination, a private channel's member list — can be recorded independently. The connector
itself runs for real: its own request helpers, retry decorators, ACL translation and entity
construction, through the same bridge the sync engine uses.

What these do not prove is that the recorded payloads still match what Slack sends today.
Only a live workspace shows that.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import pytest

from knowledge_index.connectors.bridge import ConnectorAdapter, LoopRunner
from knowledge_index.connectors.configs import SlackConfig
from knowledge_index.connectors.entities.slack import SlackChannelEntity, SlackMessageEntity
from knowledge_index.connectors.registry import catalog, get
from knowledge_index.connectors.runtime.errors import SourceAuthError, SourceError
from knowledge_index.connectors.runtime.files import FileService
from knowledge_index.connectors.runtime.logging import ContextualLogger
from knowledge_index.connectors.runtime.oauth import get_provider
from knowledge_index.connectors.runtime.tokens import StaticTokenProvider

TOKEN = "xoxb-test-token"
WORKSPACE_URL = "https://kanzlei.slack.com/"

# ------------------------------------------------------------------- recorded workspace

USERS: List[Dict[str, Any]] = [
    {
        "id": "U0ANNA",
        "name": "anna",
        "real_name": "Anna Achterberg",
        "profile": {"display_name": "Anna Achterberg", "email": "Anna@Kanzlei.de"},
    },
    {
        # No display_name: the connector has to fall back to real_name rather than
        # rendering an empty author.
        "id": "U0BERND",
        "name": "bernd",
        "profile": {"display_name": "", "real_name": "Bernd Bauer", "email": "bernd@kanzlei.de"},
    },
    {
        # A guest with no readable email. Present in the channel, unresolvable as a
        # principal, and therefore silently dropped from the viewer list.
        "id": "U0GAST",
        "name": "gast",
        "profile": {"display_name": "Externer Gast"},
    },
]

PUBLIC_CHANNEL = {
    "id": "C0ALLGEMEIN",
    "name": "mandate-allgemein",
    "is_private": False,
    "is_archived": False,
    "created": 1740000000,
    "num_members": 3,
    "topic": {"value": "Fristen und Termine"},
    "purpose": {"value": "Allgemeine Abstimmung"},
}
PRIVATE_CHANNEL = {
    "id": "C0MUELLER",
    "name": "mandat-mueller",
    "is_private": True,
    "is_archived": False,
    "created": 1740000100,
    "num_members": 3,
}
ARCHIVED_CHANNEL = {
    "id": "C0ALTMANDAT",
    "name": "altes-mandat",
    "is_private": False,
    "is_archived": True,
    "created": 1700000000,
}

THREAD_PARENT_TS = "1740003600.000100"

PUBLIC_MESSAGES = [
    {
        "ts": THREAD_PARENT_TS,
        "user": "U0ANNA",
        "text": "Die Frist läuft am Freitag ab, <@U0BERND> bitte prüfen. <!here>",
        "thread_ts": THREAD_PARENT_TS,
        "reply_count": 1,
    },
    {
        "ts": "1740003500.000200",
        "user": "U0BERND",
        "text": "Ich habe den <https://kanzlei.de/akte|Aktenlink> hinterlegt.",
        "files": [{"name": "Kaufvertrag.pdf"}],
        "edited": {"user": "U0BERND", "ts": "1740003550.000000"},
    },
    {
        # Join churn: present in every real history payload, worthless to index.
        "ts": "1740003400.000300",
        "subtype": "channel_join",
        "user": "U0GAST",
        "text": "<@U0GAST> has joined the channel",
    },
]

# Deliberately the same ts as the public channel's thread parent: Slack allows it, and a
# connector keyed on ts alone would lose one of the two messages.
PRIVATE_MESSAGES = [
    {
        "ts": THREAD_PARENT_TS,
        "user": "U0ANNA",
        "text": "Der Mandant Müller hat den Nachtrag unterschrieben.",
    }
]

THREAD_REPLIES = {
    "C0ALLGEMEIN": {
        THREAD_PARENT_TS: [
            PUBLIC_MESSAGES[0],
            {
                "ts": "1740003700.000100",
                "user": "U0BERND",
                "thread_ts": THREAD_PARENT_TS,
                "text": "Erledigt, der Schriftsatz ist raus.",
            },
        ]
    }
}

CHANNEL_MEMBERS = {"C0MUELLER": ["U0ANNA", "U0BERND", "U0GAST"]}

# Every Web API method this connector is allowed to touch. Kept explicit so a new call
# that needs a scope the firm never granted is caught here rather than at a customer.
PERMITTED_METHODS = frozenset(
    {
        "auth.test",
        "conversations.list",
        "conversations.history",
        "conversations.replies",
        "conversations.members",
        "users.list",
    }
)

RATE_LIMIT_ERRORS = frozenset({"ratelimited", "rate_limited"})


# ------------------------------------------------------------------------- the fake client


class FakeSlack:
    """Stands in for ``HttpClient``, answering Slack methods from a recorded workspace.

    Mirrors the real client's surface rather than being injected lower down, so the
    connector exercises its own header construction, parameter passing, ``ok`` checking
    and cursor following. Cursors are stringified offsets, which makes pagination real
    without needing hundreds of fixture messages.
    """

    def __init__(
        self,
        *,
        channels: Optional[List[Dict[str, Any]]] = None,
        history: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        replies: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = None,
        members: Optional[Dict[str, List[str]]] = None,
        users: Optional[List[Dict[str, Any]]] = None,
        errors: Optional[Dict[Any, str]] = None,
        once: Optional[Dict[str, str]] = None,
        page_size: int = 200,
        serve_archived: bool = False,
        workspace_url: str = WORKSPACE_URL,
    ) -> None:
        self.channels = channels if channels is not None else [PUBLIC_CHANNEL, PRIVATE_CHANNEL]
        self.history = history if history is not None else {
            "C0ALLGEMEIN": PUBLIC_MESSAGES,
            "C0MUELLER": PRIVATE_MESSAGES,
        }
        self.replies = replies if replies is not None else THREAD_REPLIES
        self.members = members if members is not None else CHANNEL_MEMBERS
        self.users = users if users is not None else USERS
        self.errors = errors or {}
        self.once = dict(once or {})
        self.page_size = page_size
        self.serve_archived = serve_archived
        self.workspace_url = workspace_url
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self.closed = False

    # -- HttpClient surface -------------------------------------------------

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = kwargs.get("headers") or {}
        # The connector must authenticate through the injected provider, never by
        # smuggling a token in some other way.
        assert headers.get("Authorization") == f"Bearer {TOKEN}", headers
        api_method = url.rsplit("/", 1)[-1]
        params = {key: value for key, value in (kwargs.get("params") or {}).items()}
        self.calls.append((api_method, params))
        assert api_method in PERMITTED_METHODS, f"unexpected Slack method {api_method}"

        failure = self._failure_for(api_method, params)
        if failure is not None:
            return self._response(url, {"ok": False, "error": failure}, failure=failure)
        return self._response(url, self._dispatch(api_method, params))

    async def aclose(self) -> None:
        self.closed = True

    @property
    def is_closed(self) -> bool:
        return self.closed

    def methods_called(self) -> set[str]:
        return {method for method, _params in self.calls}

    def call_count(self, method: str) -> int:
        return sum(1 for name, _params in self.calls if name == method)

    # -- internals ----------------------------------------------------------

    def _failure_for(self, method: str, params: Dict[str, Any]) -> Optional[str]:
        if method in self.once:
            return self.once.pop(method)
        for key in ((method, params.get("channel")), method):
            if key in self.errors:
                return self.errors[key]
        return None

    def _response(self, url: str, payload: dict, failure: str | None = None) -> httpx.Response:
        headers = {"Retry-After": "1"} if failure in RATE_LIMIT_ERRORS else {}
        # Always 200, exactly as Slack does: the error lives in the body.
        return httpx.Response(
            200, json=payload, headers=headers, request=httpx.Request("GET", url)
        )

    def _dispatch(self, method: str, params: Dict[str, Any]) -> dict:
        if method == "auth.test":
            return {
                "ok": True,
                "url": self.workspace_url,
                "team": "Kanzlei",
                "team_id": "T0KANZLEI",
                "user_id": "U0ANNA",
            }
        if method == "users.list":
            return self._page(self.users, params, "members")
        if method == "conversations.list":
            return self._page(self._visible_channels(params), params, "channels")
        if method == "conversations.history":
            return self._page(self.history.get(params.get("channel"), []), params, "messages")
        if method == "conversations.replies":
            per_channel = self.replies.get(params.get("channel"), {})
            return self._page(per_channel.get(params.get("ts"), []), params, "messages")
        if method == "conversations.members":
            channel = params.get("channel")
            if channel not in self.members:
                return {"ok": False, "error": "channel_not_found"}
            return self._page(self.members[channel], params, "members")
        raise AssertionError(f"the fake has no recording for {method}")

    def _visible_channels(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        wanted = str(params.get("types") or "public_channel").split(",")
        visible = []
        for channel in self.channels:
            kind = "private_channel" if channel.get("is_private") else "public_channel"
            if kind not in wanted:
                continue
            excluding = str(params.get("exclude_archived", "")).lower() == "true"
            if channel.get("is_archived") and excluding and not self.serve_archived:
                continue
            visible.append(channel)
        return visible

    def _page(self, items: List[Any], params: Dict[str, Any], key: str) -> dict:
        limit = min(int(params.get("limit") or 200), self.page_size)
        start = int(params.get("cursor") or 0)
        window = items[start : start + limit]
        following = start + limit
        return {
            "ok": True,
            key: window,
            "response_metadata": {
                "next_cursor": str(following) if following < len(items) else ""
            },
        }


# ------------------------------------------------------------------------------ harness


def _build_source(fake: FakeSlack, **config: Any):
    source_class = get("slack").load()
    return source_class.create(
        auth=StaticTokenProvider(TOKEN),
        logger=ContextualLogger(source="slack", run_id="test"),
        http_client=fake,
        config=SlackConfig(**config),
    )


def entities(fake: FakeSlack, **config: Any) -> List[Any]:
    """Drain ``generate_entities()`` into a list."""

    async def run() -> List[Any]:
        source = await _build_source(fake, **config)
        return [entity async for entity in source.generate_entities()]

    return asyncio.run(run())


def messages(fake: FakeSlack, **config: Any) -> List[SlackMessageEntity]:
    return [e for e in entities(fake, **config) if isinstance(e, SlackMessageEntity)]


def channels(fake: FakeSlack, **config: Any) -> List[SlackChannelEntity]:
    return [e for e in entities(fake, **config) if isinstance(e, SlackChannelEntity)]


def adapter(fake: FakeSlack, staging, **config: Any) -> ConnectorAdapter:
    """A real bridge adapter over the connector, built the way the registry builds one."""
    runner = LoopRunner()
    try:
        source = runner.run(_build_source(fake, **config))
    except BaseException:
        runner.close()
        raise
    return ConnectorAdapter(
        "slack",
        source,
        file_service=FileService(staging, run_id="slack"),
        runner=runner,
        http_client=fake,
        logger=ContextualLogger(source="slack", run_id="test"),
    )


# ---------------------------------------------------------------------------- traversal


def test_channels_and_messages_are_yielded():
    fake = FakeSlack()
    produced = entities(fake)

    assert [c.channel_name for c in produced if isinstance(c, SlackChannelEntity)] == [
        "mandate-allgemein",
        "mandat-mueller",
    ]
    posted = [m for m in produced if isinstance(m, SlackMessageEntity)]
    assert len(posted) == 3  # two public (the join event is churn), one private
    # A channel is a container: it exists for breadcrumbs and access, and must not be
    # indexed as a document of its own.
    assert all(not c.textual_representation for c in produced if isinstance(c, SlackChannelEntity))
    assert all(m.textual_representation for m in posted)


def test_channel_container_carries_topic_and_purpose():
    (public, private) = channels(FakeSlack())
    assert public.topic == "Fristen und Termine"
    assert public.purpose == "Allgemeine Abstimmung"
    assert public.is_private is False
    assert private.is_private is True
    assert public.created_at is not None


def test_message_entity_ids_are_stable_and_unique():
    ids = [m.id for m in messages(FakeSlack())]
    assert len(ids) == len(set(ids)), "duplicate entity ids would overwrite messages"
    # The same ts in two channels: only the channel-qualified key keeps both.
    assert f"C0ALLGEMEIN:{THREAD_PARENT_TS}" in ids
    assert f"C0MUELLER:{THREAD_PARENT_TS}" in ids
    # Stable across runs, or every sync would re-add the whole workspace.
    assert [m.id for m in messages(FakeSlack())] == ids


def test_join_and_leave_churn_is_not_indexed():
    texts = [m.text for m in messages(FakeSlack())]
    assert not any("has joined the channel" in text for text in texts)


def test_archived_channels_are_skipped_even_when_the_api_returns_them():
    fake = FakeSlack(
        channels=[PUBLIC_CHANNEL, ARCHIVED_CHANNEL],
        history={"C0ALLGEMEIN": PUBLIC_MESSAGES, "C0ALTMANDAT": PUBLIC_MESSAGES},
        serve_archived=True,
    )
    assert [c.id for c in channels(fake)] == ["C0ALLGEMEIN"]
    assert all(m.channel_id == "C0ALLGEMEIN" for m in messages(fake))


def test_private_channels_can_be_excluded_by_config():
    fake = FakeSlack()
    produced = channels(fake, include_private_channels=False)
    assert [c.id for c in produced] == ["C0ALLGEMEIN"]
    types = next(params["types"] for method, params in fake.calls if method == "conversations.list")
    assert "private_channel" not in types


def test_history_pagination_follows_the_cursor():
    # One message per page: the connector only sees all three if it follows next_cursor.
    fake = FakeSlack(page_size=1)
    assert len(messages(fake)) == 3
    assert fake.call_count("conversations.history") > 2


def test_max_messages_per_channel_caps_the_crawl():
    fake = FakeSlack()
    capped = messages(fake, max_messages_per_channel=1)
    assert len(capped) == 2  # one per channel
    assert {m.channel_id for m in capped} == {"C0ALLGEMEIN", "C0MUELLER"}


# --------------------------------------------------------------------------- rendering


def test_user_mentions_are_humanised():
    posted = {m.id: m for m in messages(FakeSlack())}
    message = posted[f"C0ALLGEMEIN:{THREAD_PARENT_TS}"]
    # "<@U0BERND>" matches no query anybody would type.
    assert "<@U0BERND>" not in message.text
    assert "@Bernd Bauer" in message.text
    assert "@here" in message.text
    assert "@Bernd Bauer" in (message.textual_representation or "")


def test_author_and_header_context_travel_with_the_text():
    posted = {m.id: m for m in messages(FakeSlack())}
    message = posted[f"C0ALLGEMEIN:{THREAD_PARENT_TS}"]
    assert message.author == "Anna Achterberg"
    rendered = message.textual_representation or ""
    # A message read alone is often meaningless, so channel/author/time lead the document.
    assert rendered.startswith("#mandate-allgemein — Anna Achterberg, ")
    assert "Die Frist läuft am Freitag ab" in rendered


def test_links_and_uploads_survive_into_the_indexed_text():
    posted = {m.id: m for m in messages(FakeSlack())}
    message = posted["C0ALLGEMEIN:1740003500.000200"]
    assert "Aktenlink (https://kanzlei.de/akte)" in message.text
    assert "[file: Kaufvertrag.pdf]" in message.text
    assert message.author == "Bernd Bauer"  # display_name empty, real_name used
    assert message.edited_at is not None


def test_thread_replies_are_folded_into_the_message_that_started_the_thread():
    fake = FakeSlack()
    posted = {m.id: m for m in messages(fake)}
    parent = posted[f"C0ALLGEMEIN:{THREAD_PARENT_TS}"]
    assert "Erledigt, der Schriftsatz ist raus." in (parent.textual_representation or "")
    assert "Bernd Bauer" in (parent.thread_excerpt or "")
    # The reply is context on the parent, not a separate document that retrieves alone.
    assert "C0ALLGEMEIN:1740003700.000100" not in posted


def test_thread_replies_can_be_switched_off():
    fake = FakeSlack()
    posted = {m.id: m for m in messages(fake, include_thread_replies=False)}
    assert posted[f"C0ALLGEMEIN:{THREAD_PARENT_TS}"].thread_excerpt is None
    assert fake.call_count("conversations.replies") == 0


def test_permalinks_are_built_from_the_workspace_url_without_extra_calls():
    fake = FakeSlack()
    posted = {m.id: m for m in messages(fake)}
    assert posted[f"C0ALLGEMEIN:{THREAD_PARENT_TS}"].permalink == (
        f"https://kanzlei.slack.com/archives/C0ALLGEMEIN/p{THREAD_PARENT_TS.replace('.', '')}"
    )
    # One auth.test for the whole run, not a chat.getPermalink per message.
    assert fake.call_count("auth.test") == 1


# ------------------------------------------------------------------------ access control


def test_public_channels_are_readable_by_the_whole_workspace():
    produced = entities(FakeSlack())
    public = [e for e in produced if getattr(e, "channel_id", e.id) == "C0ALLGEMEIN"]
    assert public
    for entity in public:
        assert entity.access is not None
        assert entity.access.is_public is True
        # Enumerating the workspace would be large and stale the moment somebody is hired.
        assert entity.access.viewers == []


def test_private_channel_membership_resolves_to_member_emails():
    private = [m for m in messages(FakeSlack()) if m.channel_id == "C0MUELLER"]
    assert private
    access = private[0].access
    assert access is not None
    assert access.is_public is False
    # The guest has no readable email and is dropped rather than guessed at.
    assert access.viewers == ["user:anna@kanzlei.de", "user:bernd@kanzlei.de"]


def test_unreadable_private_membership_stays_unknown_rather_than_empty():
    # Fail-closed: None is "we could not read this", which the permission compiler treats
    # as a capability gap. An empty AccessControl would instead assert that a channel the
    # firm is actively using may be read by nobody.
    fake = FakeSlack(errors={("conversations.members", "C0MUELLER"): "channel_not_found"})
    private = [m for m in messages(fake) if m.channel_id == "C0MUELLER"]
    assert private
    assert private[0].access is None


def test_membership_without_resolvable_emails_stays_unknown():
    fake = FakeSlack(members={"C0MUELLER": ["U0GAST"]})
    private = [m for m in messages(fake) if m.channel_id == "C0MUELLER"]
    assert private[0].access is None


def test_a_dead_user_directory_leaves_private_access_unknown_but_keeps_public_channels():
    fake = FakeSlack(errors={"users.list": "missing_scope"})
    produced = messages(fake)
    assert [m.access is None for m in produced if m.channel_id == "C0MUELLER"] == [True]
    # Public channels are unaffected: their access does not depend on the directory.
    assert all(
        m.access is not None and m.access.is_public
        for m in produced
        if m.channel_id == "C0ALLGEMEIN"
    )


# --------------------------------------------------------------------- failure handling


def test_one_broken_channel_does_not_abort_the_others():
    fake = FakeSlack(errors={("conversations.history", "C0ALLGEMEIN"): "channel_not_found"})
    produced = messages(fake)
    # The failing channel contributes nothing; the rest of the workspace still arrives.
    assert [m.channel_id for m in produced] == ["C0MUELLER"]
    assert [c.id for c in channels(fake)] == ["C0ALLGEMEIN", "C0MUELLER"]


def test_not_in_channel_is_a_skip_not_a_failure():
    fake = FakeSlack(errors={("conversations.history", "C0MUELLER"): "not_in_channel"})
    assert {m.channel_id for m in messages(fake)} == {"C0ALLGEMEIN"}


def test_invalid_auth_raises_source_auth_error_despite_the_200_status():
    # The whole point: Slack reports dead credentials inside a 200 body. Reading that as
    # an empty workspace would let the engine tombstone the firm's Slack corpus.
    fake = FakeSlack(errors={"conversations.list": "invalid_auth"})
    with pytest.raises(SourceAuthError):
        entities(fake)


@pytest.mark.parametrize("error", ["invalid_auth", "account_inactive", "token_revoked"])
def test_every_dead_credential_error_aborts_validate(error):
    fake = FakeSlack(errors={"auth.test": error})

    async def run() -> None:
        source = await _build_source(fake)
        await source.validate()

    with pytest.raises(SourceAuthError):
        asyncio.run(run())


def test_validate_pings_auth_test():
    fake = FakeSlack()

    async def run() -> None:
        source = await _build_source(fake)
        await source.validate()

    asyncio.run(run())
    assert fake.methods_called() == {"auth.test"}


def test_an_unrecognised_ok_false_error_is_a_source_error_not_silence():
    fake = FakeSlack(errors={"conversations.list": "internal_error"})
    with pytest.raises(SourceError) as raised:
        entities(fake)
    assert "internal_error" in str(raised.value)
    assert not isinstance(raised.value, SourceAuthError)


def test_a_rate_limited_call_is_retried_rather_than_lost():
    # "ratelimited" arrives as ok:false too, so it has to be routed to the retry path by
    # hand; otherwise the first throttle silently truncates a channel.
    fake = FakeSlack(once={"conversations.list": "ratelimited"})
    assert len(messages(fake)) == 3
    assert fake.call_count("conversations.list") >= 2


# ---------------------------------------------------------------------------- the bridge


def test_messages_are_staged_as_text_the_pipeline_can_read(tmp_path):
    connector = adapter(FakeSlack(), tmp_path)
    try:
        observations = list(connector.full_scan())
    finally:
        connector.close()

    # Channels are containers, so only the three messages become observations.
    assert len(observations) == 3
    staged = [o for o in observations if o.staged_path]
    assert len(staged) == 3
    contents = []
    for observation in staged:
        with open(observation.staged_path, encoding="utf-8") as handle:
            contents.append(handle.read())
    assert any("Die Frist läuft am Freitag ab" in text for text in contents)
    assert any("Nachtrag unterschrieben" in text for text in contents)
    assert all(o.mtime is not None for o in observations)
    assert all(o.path.startswith("#") for o in observations)  # breadcrumb is the channel


def test_the_bridge_translates_channel_privacy_into_grants(tmp_path):
    connector = adapter(FakeSlack(), tmp_path)
    try:
        observations = {o.external_id: o for o in connector.full_scan()}
    finally:
        connector.close()

    public = observations[f"C0ALLGEMEIN:{THREAD_PARENT_TS}"]
    assert [grant["principal"] for grant in public.acl] == ["role:authenticated"]

    private = observations[f"C0MUELLER:{THREAD_PARENT_TS}"]
    principals = {grant["principal"] for grant in private.acl}
    assert principals == {"user:anna@kanzlei.de", "user:bernd@kanzlei.de"}
    assert "role:authenticated" not in principals  # a private channel is not workspace-wide


def test_unknown_access_reaches_the_engine_as_unknown(tmp_path):
    fake = FakeSlack(errors={("conversations.members", "C0MUELLER"): "channel_not_found"})
    connector = adapter(fake, tmp_path)
    try:
        observations = {o.external_id: o for o in connector.full_scan()}
    finally:
        connector.close()
    # None, not []: the difference between "we could not read it" and "nobody may read it".
    assert observations[f"C0MUELLER:{THREAD_PARENT_TS}"].acl is None


def test_slack_has_no_groups_left_to_expand(tmp_path):
    # Channel membership is already a flat member list, so there is nothing for the
    # permission compiler to expand and no synthetic per-channel group is invented.
    connector = adapter(FakeSlack(), tmp_path)
    try:
        list(connector.full_scan())
        assert connector.memberships() == []
        assert connector.capabilities.acl is True
    finally:
        connector.close()


# ------------------------------------------------------------------- catalog and scopes


def test_the_catalog_advertises_slack_as_mirroring_acls():
    entries = {entry["id"]: entry for entry in catalog()}
    assert entries["slack"]["name"] == "Slack"
    assert entries["slack"]["category"] == "Messaging"
    assert entries["slack"]["acl_sync"] is True
    assert entries["slack"]["needs_oauth"] is True


def test_the_catalog_entry_resolves_to_the_source_class():
    source_class = get("slack").load()
    assert source_class.is_source is True
    assert source_class.short_name == "slack"
    assert source_class.supports_access_control is True
    assert source_class.config_class is SlackConfig


def test_the_connector_only_calls_methods_the_granted_scopes_cover():
    fake = FakeSlack()
    entities(fake)
    called = fake.methods_called()
    assert called <= PERMITTED_METHODS
    # And the scopes those methods need are the ones actually requested at authorization.
    scope = set((get_provider("slack").scope or "").split())
    assert {
        "channels:read",
        "channels:history",
        "groups:read",
        "groups:history",
        "users:read",
        "users:read.email",
    } <= scope
    # This connector indexes; it must not be relying on the federated-search scope.
    assert "search:read" not in scope
    # Uploaded file names arrive in message history. The connector does not download
    # Slack file bytes, so asking for files:read would be unnecessary access.
    assert "files:read" not in scope
