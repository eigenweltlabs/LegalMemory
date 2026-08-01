"""Gmail entity schemas.

Defines entity schemas for Gmail resources:
  - Thread
  - Message
  - Attachment
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from pydantic import computed_field

from knowledge_index.connectors.entities._field import IndexField
from knowledge_index.connectors.entities._base import (
    BaseEntity,
    Breadcrumb,
    DeletionEntity,
    EmailEntity,
    FileEntity,
)


def _parse_header(headers: List[Dict[str, str]], name: str) -> Optional[str]:
    """Return the value of the first header matching *name* (case-insensitive)."""
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value")
    return None


def _parse_address_list(value: Optional[str]) -> List[str]:
    """Split a comma-separated address header into a trimmed list."""
    if not value:
        return []
    return [addr.strip() for addr in value.split(",")]


def _parse_rfc2822_date(value: Optional[str]) -> Optional[datetime]:
    """Parse an RFC 2822 Date header as an aware UTC datetime; *None* on failure.

    A Date header without an offset parses as naive. Every other timestamp on this
    entity is aware, and one naive value among them is what makes a later comparison
    raise, so a missing offset is read as UTC rather than left unlabelled.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _internal_date_to_datetime(ms: Optional[str]) -> Optional[datetime]:
    """Convert Gmail's internalDate (epoch millis string) to a datetime."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


class GmailThreadEntity(BaseEntity):
    """Schema for Gmail thread entities.

    Reference: https://developers.google.com/gmail/api/reference/rest/v1/users.threads
    """

    thread_key: str = IndexField(
        ...,
        description="Stable thread key (thread_<gmail_id>)",
        is_entity_id=True,
    )
    gmail_thread_id: str = IndexField(
        ..., description="Native Gmail thread ID", embeddable=False
    )
    title: str = IndexField(
        ...,
        description="Display title derived from snippet",
        is_name=True,
        embeddable=True,
    )
    last_message_at: Optional[datetime] = IndexField(
        None,
        description="Timestamp of the most recent message in the thread",
        is_updated_at=True,
    )
    snippet: Optional[str] = IndexField(
        None, description="A short snippet from the thread", embeddable=True
    )
    history_id: Optional[str] = IndexField(
        None, description="The thread's history ID", embeddable=False
    )
    message_count: Optional[int] = IndexField(
        0, description="Number of messages in the thread", embeddable=False
    )
    label_ids: List[str] = IndexField(
        default_factory=list, description="Labels applied to this thread", embeddable=True
    )

    @classmethod
    def from_api(
        cls,
        data: Dict[str, Any],
        *,
        thread_id: str,
    ) -> GmailThreadEntity:
        """Build from a Gmail API thread detail JSON object.

        Args:
            data: Full thread resource from ``users.threads.get``.
            thread_id: The Gmail thread ID.
        """
        snippet = data.get("snippet", "")
        history_id = data.get("historyId")
        messages = data.get("messages", []) or []

        message_count = len(messages)
        last_message_at: Optional[datetime] = None
        if messages:
            sorted_msgs = sorted(
                messages, key=lambda m: int(m.get("internalDate", 0)), reverse=True
            )
            last_message_at = _internal_date_to_datetime(sorted_msgs[0].get("internalDate"))

        label_ids = messages[0].get("labelIds", []) if messages else []
        title = snippet[:50] + "..." if len(snippet) > 50 else snippet or "Thread"

        return cls(
            breadcrumbs=[],
            thread_key=f"thread_{thread_id}",
            gmail_thread_id=thread_id,
            title=title,
            last_message_at=last_message_at,
            snippet=snippet,
            history_id=history_id,
            message_count=message_count,
            label_ids=label_ids,
        )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Direct link to open the thread in Gmail."""
        return f"https://mail.google.com/mail/u/0/#inbox/{self.gmail_thread_id}"


class GmailMessageEntity(EmailEntity):
    """Schema for Gmail message entities.

    Reference: https://developers.google.com/gmail/api/reference/rest/v1/users.messages
    """

    message_key: str = IndexField(
        ...,
        description="Stable message key (msg_<gmail_id>)",
        is_entity_id=True,
    )
    message_id: str = IndexField(..., description="Native Gmail message ID", embeddable=False)
    subject: str = IndexField(
        ...,
        description="Subject line (fallback applied if missing)",
        is_name=True,
        embeddable=True,
    )
    sent_at: datetime = IndexField(
        ...,
        description="Timestamp from the Date header (or internal date fallback)",
        is_created_at=True,
    )
    internal_timestamp: datetime = IndexField(
        ...,
        description="Gmail internal timestamp representing last modification",
        is_updated_at=True,
    )
    thread_id: str = IndexField(
        ..., description="ID of the thread this message belongs to", embeddable=False
    )
    sender: Optional[str] = IndexField(
        None, description="Email address of the sender", embeddable=True
    )
    to: List[str] = IndexField(
        default_factory=list, description="Recipients of the message", embeddable=True
    )
    cc: List[str] = IndexField(
        default_factory=list, description="CC recipients", embeddable=True
    )
    bcc: List[str] = IndexField(
        default_factory=list, description="BCC recipients", embeddable=True
    )
    date: Optional[datetime] = IndexField(
        None, description="Date the message was sent", embeddable=True
    )
    snippet: Optional[str] = IndexField(
        None, description="Brief snippet of the message content", embeddable=True
    )
    label_ids: List[str] = IndexField(
        default_factory=list, description="Labels applied to this message", embeddable=True
    )
    internal_date: Optional[datetime] = IndexField(
        None, description="Internal Gmail timestamp", embeddable=False
    )
    web_url_value: Optional[str] = IndexField(
        None,
        description="Direct Gmail URL for the message",
        embeddable=False,
        unhashable=True,
    )

    @classmethod
    def from_api(
        cls,
        data: Dict[str, Any],
        *,
        thread_id: str,
        breadcrumbs: List[Breadcrumb],
    ) -> GmailMessageEntity:
        """Build from a Gmail API message detail JSON object.

        Extracts headers (subject, from, to, cc, bcc, date) and timestamps.
        Does **not** handle body extraction or file saving — that stays in the source.

        Args:
            data: Full message resource from ``users.messages.get``.
            thread_id: Parent thread ID.
            breadcrumbs: Breadcrumb chain (typically [thread_breadcrumb]).
        """
        message_id = data.get("id", "")
        internal_date = _internal_date_to_datetime(data.get("internalDate"))

        payload = data.get("payload", {}) or {}
        headers = payload.get("headers", []) or []

        subject = _parse_header(headers, "subject")
        sender = _parse_header(headers, "from")
        to_list = _parse_address_list(_parse_header(headers, "to"))
        cc_list = _parse_address_list(_parse_header(headers, "cc"))
        bcc_list = _parse_address_list(_parse_header(headers, "bcc"))
        date = _parse_rfc2822_date(_parse_header(headers, "date"))

        subject_value = subject or f"Message {message_id}"
        sent_at = date or internal_date or datetime.fromtimestamp(0, tz=timezone.utc)
        internal_ts = internal_date or sent_at
        web_url = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"

        return cls(
            breadcrumbs=breadcrumbs,
            message_key=f"msg_{message_id}",
            message_id=message_id,
            subject=subject_value,
            sent_at=sent_at,
            internal_timestamp=internal_ts,
            url=web_url,
            size=data.get("sizeEstimate", 0),
            file_type="html",
            mime_type="text/html",
            local_path=None,
            thread_id=thread_id,
            sender=sender,
            to=to_list,
            cc=cc_list,
            bcc=bcc_list,
            date=date,
            snippet=data.get("snippet"),
            label_ids=data.get("labelIds", []),
            internal_date=internal_date,
            web_url_value=web_url,
        )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Direct link to open the message in Gmail."""
        return self.web_url_value or f"https://mail.google.com/mail/u/0/#inbox/{self.message_id}"


class GmailAttachmentEntity(FileEntity):
    """Schema for Gmail attachment entities.

    Reference: https://developers.google.com/gmail/api/reference/rest/v1/users.messages.attachments
    """

    attachment_key: str = IndexField(
        ...,
        description="Stable attachment key (attach_<message>_<filename>)",
        is_entity_id=True,
    )
    filename: str = IndexField(
        ..., description="Attachment filename", is_name=True, embeddable=True
    )
    message_id: str = IndexField(
        ..., description="ID of the message this attachment belongs to", embeddable=False
    )
    attachment_id: str = IndexField(..., description="Gmail's attachment ID", embeddable=False)
    thread_id: str = IndexField(
        ..., description="ID of the thread containing the message", embeddable=False
    )
    web_url_value: Optional[str] = IndexField(
        None,
        description="URL to view the parent message in Gmail",
        embeddable=False,
        unhashable=True,
    )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Link to the parent message view in Gmail."""
        return self.web_url_value or f"https://mail.google.com/mail/u/0/#inbox/{self.message_id}"


class GmailMessageDeletionEntity(DeletionEntity):
    """Deletion signal for a Gmail message.

    Emitted when the Gmail History API reports a messageDeleted. The entity_id matches the
    message entity's ID format (msg_{message_id}) so downstream deletion removes the
    correct parent/children.
    """

    deletes_entity_class = GmailMessageEntity

    message_key: str = IndexField(
        ...,
        description="Stable message key (msg_<gmail_id>)",
        is_entity_id=True,
    )
    label: str = IndexField(
        ...,
        description="Human-readable deletion label",
        is_name=True,
        embeddable=True,
    )
    message_id: str = IndexField(
        ..., description="The Gmail message ID that was deleted", embeddable=False
    )
    thread_id: Optional[str] = IndexField(
        None,
        description="Thread ID (optional if not provided by change record)",
        embeddable=False,
    )

    @computed_field(return_type=str)
    def web_url(self) -> str:
        """Fallback link to Gmail inbox for the deleted message."""
        return f"https://mail.google.com/mail/u/0/#inbox/{self.message_id}"
