"""The ``@source`` decorator: declares a connector's identity and capabilities.

Attributes set here are read by the registry and the sync bridge — which auth methods
a source accepts, whether it can mirror ACLs, whether it has a native change feed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, List, Optional, Type, TypeVar

from pydantic import BaseModel

from knowledge_index.connectors.runtime.types import (
    AuthenticationMethod,
    OAuthType,
    RateLimitLevel,
)

if TYPE_CHECKING:
    from knowledge_index.connectors.base import BaseSource

    _SourceT = TypeVar("_SourceT", bound=BaseSource)
else:
    _SourceT = TypeVar("_SourceT")


def source(
    name: str,
    short_name: str,
    auth_methods: List[AuthenticationMethod],
    oauth_type: Optional[OAuthType] = None,
    requires_byoc: bool = False,
    auth_config_class: Optional[Type[BaseModel]] = None,
    config_class: Optional[Type[BaseModel]] = None,
    labels: Optional[List[str]] = None,
    supports_continuous: bool = False,
    federated_search: bool = False,
    supports_temporal_relevance: bool = True,
    rate_limit_level: Optional[RateLimitLevel] = None,
    cursor_class: Optional[Type[BaseModel]] = None,
    supports_access_control: bool = False,
    supports_browse_tree: bool = False,
    feature_flag: Optional[str] = None,
    internal: bool = False,
) -> Callable[[type[_SourceT]], type[_SourceT]]:
    """Enhanced source decorator with OAuth type tracking and typed cursor support.

    Args:
        name: Display name for the source
        short_name: Unique identifier for the source type
        auth_methods: List of supported authentication methods
        oauth_type: OAuth token type (for OAuth sources)
        requires_byoc: Whether this OAuth source requires user to bring their own client credentials
        auth_config_class: Pydantic model for auth configuration (for DIRECT auth only)
        config_class: Pydantic model for source configuration
        labels: Tags for categorization (e.g., "CRM", "Database")
        supports_continuous: Whether source supports cursor-based continuous syncing (default False)
        federated_search: Whether source uses federated search instead of syncing (default False)
        supports_temporal_relevance: Whether source entities have timestamps for (default True)
        cursor_class: Optional Pydantic model class for typed cursor (e.g., GmailCursor)
        rate_limit_level: Rate limiting level (RateLimitLevel.ORG, RateLimitLevel.CONNECTION,
            or None)
        supports_access_control: Whether this source provides entity-level access control
            metadata. When True, the source must:
            1. Set entity.access on all yielded entities
            2. Implement generate_access_control_memberships() method
            Default is False (entities visible to everyone).
        supports_browse_tree: Whether this source supports lazy-loaded browse tree for
            node selection. When True, the source must implement get_browse_children()
            and parse_browse_node_id(). Default is False.
        feature_flag: Optional feature flag (from FeatureFlag enum) required to access this source.
            When set, only organizations with this feature enabled can see/use the source.
        internal: Whether this is an internal/test source (default False). Internal
            sources are excluded from docs and only loaded when ENABLE_INTERNAL_SOURCES=true.

    Example:
        # OAuth source (no auth config)
        @source(
            name="Gmail",
            short_name="gmail",
            auth_methods=[AuthenticationMethod.OAUTH_BROWSER, AuthenticationMethod.OAUTH_TOKEN],
            oauth_type=OAuthType.WITH_REFRESH,
            auth_config_class=None,  # OAuth sources don't need this
            config_class=GmailConfig,
            labels=["Email"],
        )

        # Direct auth source (keeps auth config)
        @source(
            name="GitHub",
            short_name="github",
            auth_methods=[AuthenticationMethod.DIRECT],
            oauth_type=None,
            auth_config_class=GitHubAuthConfig,  # Direct auth needs this
            config_class=GitHubConfig,
            labels=["Developer Tools"],
        )

        # Source with access control (e.g., SharePoint)
        @source(
            name="SharePoint 2019 V2",
            short_name="sharepoint2019v2",
            auth_methods=[AuthenticationMethod.DIRECT],
            auth_config_class=SharePoint2019V2AuthConfig,
            config_class=SharePoint2019V2Config,
            labels=["Enterprise"],
            supports_access_control=True,  # Enables entity-level access control
        )
    """

    def decorator(cls: type[_SourceT]) -> type[_SourceT]:
        # Validate continuous sync configuration
        if supports_continuous and cursor_class is None:
            raise ValueError(
                f"Source '{short_name}' has supports_continuous=True but no cursor_class defined. "
                f"Continuous syncs require a typed cursor class (e.g., cursor_class=GmailCursor)"
            )

        # Set metadata as class attributes
        cls.is_source = True
        cls.source_name = name
        cls.short_name = short_name
        cls.auth_methods = auth_methods
        cls.oauth_type = oauth_type
        cls.requires_byoc = requires_byoc
        cls.auth_config_class = auth_config_class
        cls.config_class = config_class
        cls.labels = labels or []
        cls.supports_continuous = supports_continuous
        cls.federated_search = federated_search
        cls.supports_temporal_relevance = supports_temporal_relevance
        cls.cursor_class = cursor_class
        cls.rate_limit_level = rate_limit_level
        cls.supports_access_control = supports_access_control
        cls.supports_browse_tree = supports_browse_tree
        cls.feature_flag = feature_flag
        cls.internal = internal

        return cls

    return decorator
