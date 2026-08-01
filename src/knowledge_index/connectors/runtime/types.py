"""Small value types and enums the connectors declare against.

These are deliberately kept as data-only definitions in one place: they are the
vocabulary of the connector contract (auth modes, rate-limit scope, group membership,
browse-tree nodes) and carry no behaviour worth owning separately.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AuthenticationMethod(str, Enum):
    """How a connection obtains credentials."""

    DIRECT = "direct"  # credentials typed into the admin UI (API key, PAT, app auth)
    OAUTH_BROWSER = "oauth_browser"  # authorization-code handshake in the browser
    OAUTH_TOKEN = "oauth_token"  # a pre-obtained token pasted in
    OAUTH_BYOC = "oauth_byoc"  # browser handshake with the firm's own OAuth client
    AUTH_PROVIDER = "auth_provider"  # third-party broker — never enabled on-prem


class OAuthType(str, Enum):
    """Refresh semantics of an OAuth provider."""

    OAUTH1 = "oauth1"
    ACCESS_ONLY = "access_only"
    WITH_REFRESH = "with_refresh"
    WITH_ROTATING_REFRESH = "with_rotating_refresh"


class RateLimitLevel(str, Enum):
    """Scope a source's API quota is shared across."""

    ORG = "org"
    CONNECTION = "connection"


class FieldFlag(str, Enum):
    """Semantic field markers read off entity schemas.

    ``BaseEntity`` uses these to find each schema's id/name/timestamp fields instead of
    requiring every connector to restate them.
    """

    IS_ENTITY_ID = "is_entity_id"
    IS_NAME = "is_name"
    IS_CREATED_AT = "is_created_at"
    IS_UPDATED_AT = "is_updated_at"
    EMBEDDABLE = "embeddable"
    UNHASHABLE = "unhashable"


class MembershipTuple(BaseModel):
    """One ``(member) → (group)`` edge mirrored from a source's directory.

    This is what makes group-scoped ethical walls enforceable: a document shared with
    "Litigation" is only reachable once the members of that group are known. Nested
    groups are expected to arrive already flattened by the connector.
    """

    member_id: str = Field(description="Email for users, opaque id for groups")
    member_type: str = Field(description="'user' or 'group'")
    group_id: str = Field(description="The group this member belongs to")
    group_name: str | None = None


class BrowseNode(BaseModel):
    """A node in a source's navigable tree, for scoping a sync in the admin UI."""

    source_node_id: str
    node_type: str  # site | list | folder | file | item
    title: str
    description: str | None = None
    item_count: int | None = None
    has_children: bool = False
    node_metadata: dict[str, Any] | None = None


class NodeSelectionData(BaseModel):
    """An operator's choice to sync only part of a source."""

    source_node_id: str
    node_type: str
    node_title: str | None = None
    node_metadata: dict[str, Any] | None = None
