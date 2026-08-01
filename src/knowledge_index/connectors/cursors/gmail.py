"""Gmail cursor schema for incremental sync."""

from pydantic import Field

from ._base import BaseCursor


class GmailCursor(BaseCursor):
    """Gmail incremental sync cursor using history API.

    Gmail's History API provides incremental changes using a history ID.
    Each mailbox state is associated with a history ID that can be used
    to fetch only changes since that point.

    Reference: https://developers.google.com/gmail/api/guides/sync
    """

    history_id: str = Field(
        default="", description="Gmail history ID for tracking incremental changes"
    )
