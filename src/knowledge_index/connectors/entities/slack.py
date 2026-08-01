"""Slack entity schemas.

Entity schemas for Slack objects read through the Web API:
 - Conversation (a public or private channel — a container, not a document)
 - Message (the document: one posted message, optionally with its thread)

Two things here are load-bearing for the index rather than cosmetic.

**The entity id is ``{channel_id}:{ts}``.** A Slack message has no global id. ``ts`` is
unique within a conversation and stable for the life of the message — it is the same
value the permalink is built from — but the same ``ts`` can legitimately occur in two
conversations, so the channel has to be part of the key. Without that, one channel's
message would overwrite another's on every sync.

**``textual_representation`` carries the readable message.** The bridge stages text-only
entities from that field, so whatever is not in it is not indexed. It therefore holds
the rendered text — mentions resolved to names, links unwrapped, thread replies appended
— rather than Slack's wire format, because ``<@U012AB3CD>`` matches no query a lawyer
would type.

Reference:
  https://api.slack.com/methods/conversations.list
  https://api.slack.com/methods/conversations.history
  https://api.slack.com/methods/conversations.replies
  https://api.slack.com/reference/surfaces/formatting
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from pydantic import computed_field

from knowledge_index.connectors.entities._base import BaseEntity, Breadcrumb
from knowledge_index.connectors.entities._field import IndexField

# Slack's mrkdwn escapes. Tags are resolved before unescaping, so an author who typed a
# literal "&lt;" cannot fabricate a mention.
_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL_MENTION = re.compile(r"<!([a-z]+)(?:\^[A-Z0-9]+)?(?:\|([^>]*))?>")
_CHANNEL_LINK = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]*))?>")
_LINK = re.compile(r"<(https?://[^|>]+|mailto:[^|>]+)(?:\|([^>]*))?>")
_ESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))

# Join/leave/topic churn. Indexing it fills the corpus with "X has joined the channel",
# which matches weakly against every query about who worked on a matter.
NOISE_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "group_topic",
        "group_purpose",
        "group_name",
        "group_archive",
        "group_unarchive",
        "pinned_item",
        "unpinned_item",
    }
)


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse a Slack ``ts`` (epoch seconds with a microsecond suffix) into a datetime."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _format_when(moment: Optional[datetime]) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "unknown time"


def render_text(raw: Optional[str], user_names: Optional[Mapping[str, str]] = None) -> str:
    """Turn Slack's wire text into something a person — and a retriever — can read.

    ``<@U012AB3CD>`` becomes ``@Anna Achterberg`` when the user directory is available
    and ``@U012AB3CD`` when it is not, so an unresolvable id degrades rather than
    disappearing.
    """
    if not raw:
        return ""
    names = user_names or {}

    def mention(match: re.Match[str]) -> str:
        user_id, label = match.group(1), match.group(2)
        return f"@{names.get(user_id) or label or user_id}"

    def special(match: re.Match[str]) -> str:
        keyword, label = match.group(1), match.group(2)
        return f"@{label.lstrip('@') if label else keyword}"

    def channel(match: re.Match[str]) -> str:
        channel_id, label = match.group(1), match.group(2)
        return f"#{label or channel_id}"

    def link(match: re.Match[str]) -> str:
        url, label = match.group(1), match.group(2)
        return f"{label} ({url})" if label and label != url else url

    text = _MENTION.sub(mention, raw)
    text = _SPECIAL_MENTION.sub(special, text)
    text = _CHANNEL_LINK.sub(channel, text)
    text = _LINK.sub(link, text)
    for escaped, plain in _ESCAPES:
        text = text.replace(escaped, plain)
    return text.strip()


def _author_of(data: Mapping[str, Any], user_names: Mapping[str, str]) -> tuple[str, Optional[str]]:
    """Best available display name for a message's author, plus the raw id."""
    author_id = str(data.get("user") or data.get("bot_id") or "").strip() or None
    profile = data.get("bot_profile") or {}
    name = (
        (user_names.get(author_id) if author_id else None)
        or str(data.get("username") or "").strip()
        or str(profile.get("name") or "").strip()
        or author_id
        or "unknown"
    )
    return name, author_id


def _attachment_text(data: Mapping[str, Any], user_names: Mapping[str, str]) -> List[str]:
    """Text carried outside ``text``: attachment fallbacks, file names.

    A message whose content lives entirely in an attachment or an uploaded document is
    otherwise indexed as empty, and the thread it belongs to becomes unfindable.
    """
    extra: List[str] = []
    for attachment in data.get("attachments") or []:
        if not isinstance(attachment, Mapping):
            continue
        for key in ("fallback", "text", "title", "pretext"):
            rendered = render_text(attachment.get(key), user_names)
            if rendered:
                extra.append(rendered)
                break
    for uploaded in data.get("files") or []:
        if not isinstance(uploaded, Mapping):
            continue
        label = str(uploaded.get("title") or uploaded.get("name") or "").strip()
        if label:
            extra.append(f"[file: {label}]")
    return extra


class SlackChannelEntity(BaseEntity):
    """Schema for a Slack conversation (public or private channel).

    A container: it is yielded to give its messages breadcrumb context and to carry the
    channel's access decision, and it deliberately sets no ``textual_representation`` so
    the bridge does not index it as a document.

    Reference: https://api.slack.com/types/conversation
    """

    # Base fields are set during entity creation:
    # - entity_id (channel ID)
    # - breadcrumbs (empty — channels are top-level in a workspace)
    # - name (from channel_name)
    # - created_at (from the channel's `created` epoch)
    # - updated_at (None — Slack reports no channel modification time)

    id: str = IndexField(
        ...,
        description="Slack conversation ID (e.g. 'C012AB3CD').",
        is_entity_id=True,
    )
    channel_name: str = IndexField(
        ...,
        description="Channel name without the leading '#'.",
        embeddable=True,
        is_name=True,
    )
    topic: Optional[str] = IndexField(
        None, description="The channel topic.", embeddable=True
    )
    purpose: Optional[str] = IndexField(
        None, description="The channel purpose.", embeddable=True
    )
    is_private: bool = IndexField(
        False,
        description="Whether this is a private channel. Public channels are readable "
        "by everyone in the workspace.",
        embeddable=False,
    )
    is_archived: bool = IndexField(
        False, description="Whether the channel is archived.", embeddable=False
    )
    num_members: Optional[int] = IndexField(
        None, description="Member count as reported by conversations.list.", embeddable=False
    )
    web_url_override: Optional[str] = IndexField(
        None,
        description="Link to the channel in Slack.",
        embeddable=False,
        unhashable=True,
    )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Return best-effort link to open the channel."""
        if self.web_url_override:
            return self.web_url_override
        return f"https://slack.com/app_redirect?channel={self.id}"

    @classmethod
    def from_api(
        cls,
        data: Mapping[str, Any],
        *,
        workspace_url: Optional[str] = None,
    ) -> "SlackChannelEntity":
        """Construct from a Slack ``conversation`` object."""
        channel_id = str(data["id"])
        name = str(data.get("name") or data.get("name_normalized") or channel_id)
        created = data.get("created")
        return cls(
            breadcrumbs=[],
            id=channel_id,
            name=name,
            created_at=parse_ts(str(created)) if created else None,
            updated_at=None,
            channel_name=name,
            topic=str((data.get("topic") or {}).get("value") or "").strip() or None,
            purpose=str((data.get("purpose") or {}).get("value") or "").strip() or None,
            is_private=bool(data.get("is_private")),
            is_archived=bool(data.get("is_archived")),
            num_members=data.get("num_members"),
            web_url_override=(
                f"{workspace_url.rstrip('/')}/archives/{channel_id}" if workspace_url else None
            ),
        )

    def breadcrumb(self) -> Breadcrumb:
        """The breadcrumb this channel contributes to its messages."""
        return Breadcrumb(
            entity_id=self.id,
            name=f"#{self.channel_name}",
            entity_type="SlackChannelEntity",
        )


class SlackMessageEntity(BaseEntity):
    """Schema for one Slack message, with its thread replies folded in.

    Reference: https://api.slack.com/events/message
    """

    # Base fields are set during entity creation:
    # - entity_id (f"{channel_id}:{ts}")
    # - breadcrumbs (the channel)
    # - name (a preview of the message text)
    # - created_at / updated_at (post time and edit time)
    # - textual_representation (the readable message, including thread replies)
    # - access (inherited from the channel)

    id: str = IndexField(
        ...,
        description="Stable composite id: '{channel_id}:{ts}'. Slack messages have no "
        "workspace-wide id, and ts alone repeats across channels.",
        is_entity_id=True,
    )
    preview: str = IndexField(
        ...,
        description="Short display label for the message.",
        embeddable=True,
        is_name=True,
    )
    channel_id: str = IndexField(
        ..., description="ID of the conversation the message was posted in.", embeddable=False
    )
    channel_name: str = IndexField(
        ..., description="Name of the conversation the message was posted in.", embeddable=True
    )
    ts: str = IndexField(
        ..., description="Slack message timestamp — unique within the channel.", embeddable=False
    )
    thread_ts: Optional[str] = IndexField(
        None,
        description="Timestamp of the thread's parent message, when the message is "
        "part of a thread.",
        embeddable=False,
    )
    author: Optional[str] = IndexField(
        None, description="Display name of the message author.", embeddable=True
    )
    author_id: Optional[str] = IndexField(
        None, description="Slack user or bot id of the author.", embeddable=False
    )
    subtype: Optional[str] = IndexField(
        None, description="Slack message subtype, when present.", embeddable=False
    )
    text: str = IndexField(
        "",
        description="The message text, with mentions and links resolved.",
        embeddable=True,
    )
    thread_excerpt: Optional[str] = IndexField(
        None,
        description="Rendered thread replies, when replies were fetched.",
        embeddable=True,
    )
    reply_count: int = IndexField(
        0, description="Number of replies in the message's thread.", embeddable=False
    )
    posted_at: Optional[datetime] = IndexField(
        None,
        description="When the message was posted.",
        embeddable=False,
        is_created_at=True,
    )
    edited_at: Optional[datetime] = IndexField(
        None,
        description="When the message was last edited.",
        embeddable=False,
        is_updated_at=True,
    )
    permalink: Optional[str] = IndexField(
        None,
        description="Link to the message in Slack.",
        embeddable=False,
        unhashable=True,
    )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Return best-effort link to open the message."""
        if self.permalink:
            return self.permalink
        return f"https://slack.com/app_redirect?channel={self.channel_id}"

    @classmethod
    def from_api(
        cls,
        data: Mapping[str, Any],
        *,
        channel_id: str,
        channel_name: str,
        breadcrumbs: List[Breadcrumb],
        user_names: Optional[Mapping[str, str]] = None,
        replies: Optional[List[Dict[str, Any]]] = None,
        permalink: Optional[str] = None,
        access: Any = None,
    ) -> "SlackMessageEntity":
        """Construct from a Slack ``message`` object plus its optional thread replies."""
        names = dict(user_names or {})
        ts = str(data["ts"])
        author, author_id = _author_of(data, names)
        body_parts = [render_text(data.get("text"), names), *_attachment_text(data, names)]
        text = "\n".join(part for part in body_parts if part)
        posted_at = parse_ts(ts)
        thread_excerpt = _render_thread(replies, names, parent_ts=ts)

        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        preview = (first_line[:80] + "…") if len(first_line) > 80 else first_line
        if not preview:
            preview = f"Message {ts}"

        return cls(
            breadcrumbs=list(breadcrumbs),
            id=f"{channel_id}:{ts}",
            name=preview,
            created_at=posted_at,
            updated_at=parse_ts(str((data.get("edited") or {}).get("ts") or "")) or posted_at,
            textual_representation=_render_document(
                channel_name=channel_name,
                author=author,
                posted_at=posted_at,
                text=text,
                thread_excerpt=thread_excerpt,
            ),
            access=access,
            preview=preview,
            channel_id=channel_id,
            channel_name=channel_name,
            ts=ts,
            thread_ts=str(data.get("thread_ts")) if data.get("thread_ts") else None,
            author=author,
            author_id=author_id,
            subtype=str(data.get("subtype")) if data.get("subtype") else None,
            text=text,
            thread_excerpt=thread_excerpt,
            reply_count=int(data.get("reply_count") or (len(replies) if replies else 0)),
            posted_at=posted_at,
            edited_at=parse_ts(str((data.get("edited") or {}).get("ts") or "")),
            permalink=permalink,
        )


def _render_thread(
    replies: Optional[List[Dict[str, Any]]],
    user_names: Mapping[str, str],
    *,
    parent_ts: str,
) -> Optional[str]:
    """Render a thread's replies, excluding the parent message itself."""
    if not replies:
        return None
    lines: List[str] = []
    for reply in replies:
        if not isinstance(reply, Mapping):
            continue
        reply_ts = str(reply.get("ts") or "")
        if not reply_ts or reply_ts == parent_ts:
            continue
        if str(reply.get("subtype") or "") in NOISE_SUBTYPES:
            continue
        author, _ = _author_of(reply, user_names)
        body_parts = [
            render_text(reply.get("text"), user_names),
            *_attachment_text(reply, user_names),
        ]
        body = " ".join(part for part in body_parts if part)
        if not body:
            continue
        lines.append(f"{author}, {_format_when(parse_ts(reply_ts))}: {body}")
    return "\n".join(lines) or None


def _render_document(
    *,
    channel_name: str,
    author: str,
    posted_at: Optional[datetime],
    text: str,
    thread_excerpt: Optional[str],
) -> str:
    """Build the text the pipeline indexes.

    The channel, author and time lead because a message read in isolation is often
    meaningless — "yes, do it" is only useful once you know who said it, where, and when.
    """
    header = f"#{channel_name} — {author}, {_format_when(posted_at)}"
    sections = [header, text or "(no text)"]
    if thread_excerpt:
        sections.append(f"Thread replies:\n{thread_excerpt}")
    return "\n\n".join(sections).strip()
