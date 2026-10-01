"""Per-connector configuration schemas."""

from __future__ import annotations

from typing import Optional
from pydantic import Field, field_validator
from knowledge_index.connectors.config_base import BaseConfig
from knowledge_index.connectors.ssrf import validate_url


class SourceConfig(BaseConfig):
    """Source config schema."""

    pass


class BoxConfig(SourceConfig):
    """Box configuration schema."""

    folder_id: str = Field(
        default="0",
        title="Folder ID",
        description=(
            "Specific Box folder ID to sync. Default is '0' (root folder, syncs all files). "
            "To sync a specific folder, enter its folder ID. "
            "You can find folder IDs in the Box URL when viewing a folder."
        ),
    )

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Box Collaborations",
        description=(
            "Read each file's collaborations so the file is retrievable by the same "
            "people who can open it in Box. Costs one extra API call per file. When "
            "false, permissions are left unknown and documents stay invisible until "
            "an administrator grants access at the project level."
        ),
    )


class ClioConfig(SourceConfig):
    """Clio configuration schema."""

    api_base_url: str = Field(
        default="https://eu.app.clio.com/api/v4",
        title="API base URL",
        description=(
            "Clio region API base. Tokens are region-bound: EU firms use "
            "https://eu.app.clio.com/api/v4; US would be https://app.clio.com/api/v4. "
            "Must match the region the OAuth application was registered in."
        ),
    )

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url_ssrf(cls, v: str) -> str:
        """Validate the region URL for SSRF safety."""
        validate_url(v.strip())
        return v.strip().rstrip("/")

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Matter Permissions",
        description=(
            "Mirror Clio's matter and document permission groups so a restricted "
            "matter's documents are retrievable only by that group's members; "
            "unrestricted content is readable by every authenticated user of this "
            "single-firm appliance. When false, permissions are left unknown and "
            "documents stay invisible until an administrator grants access at the "
            "project level."
        ),
    )


class IManageConfig(SourceConfig):
    """iManage Work configuration schema."""

    api_base_url: str = Field(
        default="https://cloudimanage.com",
        title="iManage host",
        description=(
            "The firm's iManage Work host. iManage Cloud tenants use "
            "https://cloudimanage.com; a firm on its own infrastructure or in a "
            "regional cloud has its own hostname. No trailing path — the connector "
            "appends /work/api/v2 itself."
        ),
    )

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url_ssrf(cls, v: str) -> str:
        """Validate the host for SSRF safety."""
        validate_url(v.strip())
        return v.strip().rstrip("/")

    customer_id: str = Field(
        default="",
        title="Customer ID",
        description=(
            "The firm's iManage customer id, which every API path is scoped by. Leave "
            "blank to read it from the authorizing account at connect time; set it "
            "only where that lookup is not available."
        ),
    )

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror iManage Security",
        description=(
            "Mirror workspace, folder and document security so content is retrievable "
            "only by the users and groups that hold read access in iManage, with "
            "groups expanded to their members. When false, access is left unknown and "
            "nothing is retrievable until an administrator grants it at the project "
            "level."
        ),
    )

    read_document_security: bool = Field(
        default=True,
        title="Read per-document security where it differs",
        description=(
            "iManage states on each document whether it inherits its container's "
            "security or overrides it. When a document overrides, this reads that "
            "document's own access list — one extra API call for that document only, "
            "not for the whole estate. Turning this off makes an overriding document "
            "fail-closed instead, which is safe but hides it; it never falls back to "
            "the container, because an override usually exists in order to be "
            "narrower."
        ),
    )


class ConfluenceConfig(SourceConfig):
    """Confluence configuration schema."""

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Page Restrictions",
        description=(
            "Read each page's read restrictions so the page is retrievable by the same "
            "people who can open it in Confluence. Costs one extra API call per page or "
            "blog post. A page with no restrictions is readable by anyone who can see "
            "the space, which is mirrored as such. When false, permissions are left "
            "unknown and documents stay invisible until an administrator grants access "
            "at the project level."
        ),
    )


class DropboxConfig(SourceConfig):
    """Dropbox configuration schema."""

    exclude_path: str = Field(
        default="",
        title="Exclude Path",
        description=(
            "Path prefix to exclude from sync (e.g., '/archive'). If empty, nothing is excluded."
        ),
    )

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Sharing Members",
        description=(
            "Read sharing members so each file is retrievable by the same people who can "
            "open it in Dropbox. Members are read once per shared folder and reused for "
            "everything inside it, so the cost is one call per shared folder rather than "
            "one per file. When false, permissions are left unknown and documents stay "
            "invisible until an administrator grants access at the project level."
        ),
    )

    expand_team_groups: bool = Field(
        default=True,
        title="Expand Dropbox Groups",
        description=(
            "Resolve the members of Dropbox groups named in a file's sharing members, so "
            "a folder shared with a group is retrievable by the people in that group "
            "rather than by nobody. Requires a Dropbox Business team token with "
            "groups.read; on a personal account the calls are skipped after the first "
            "refusal and group grants stay unmatched."
        ),
    )

    index_team_space: bool = Field(
        default=True,
        title="Index The Team Space",
        description=(
            "With a Dropbox Business team token, index the team's shared space — the team "
            "folders everyone works out of — rather than the home directory of the single "
            "member the token acts as. The team space is what a firm means by its file "
            "server. Turn it off to index only that member's own Dropbox. A user token "
            "can reach nothing but its own account, so this does not apply to one."
        ),
    )

    act_as_email: str = Field(
        default="",
        title="Act As Member",
        description=(
            "Email address of the Dropbox Business team member this connection acts as. "
            "Leave blank to act as the team admin who authorized the token. A team token "
            "cannot read a file without naming a member to read it as, so this decides "
            "whose view of the estate is indexed: whose home directory when the team "
            "space is off, and whose access to the team folders when it is on. Ignored "
            "by a user token."
        ),
    )


class GmailConfig(SourceConfig):
    """Gmail configuration schema."""

    after_date: Optional[str] = Field(
        None,
        title="After Date",
        description="Sync emails after this date (format: YYYY/MM/DD or YYYY-MM-DD).",
    )

    included_labels: list[str] = Field(
        default=["inbox", "sent"],
        title="Included Labels",
        description=(
            "Labels to include (e.g., 'inbox', 'sent', 'important'). Defaults to inbox and sent."
        ),
    )

    excluded_labels: list[str] = Field(
        default=[
            "spam",
            "trash",
        ],
        title="Excluded Labels",
        description=(
            "Labels to exclude (e.g., 'spam', 'trash', 'promotions', 'social'). "
            "Defaults to spam and trash."
        ),
    )

    excluded_categories: list[str] = Field(
        default=["promotions", "social"],
        title="Excluded Categories",
        description=(
            "Gmail categories to exclude (e.g., 'promotions', 'social', 'updates', 'forums')."
        ),
    )

    gmail_query: Optional[str] = Field(
        None,
        title="Custom Gmail Query",
        description=(
            "Advanced. Custom Gmail query string (overrides all other filters if provided)."
        ),
    )

    @field_validator("included_labels", "excluded_labels", "excluded_categories", mode="before")
    @classmethod
    def parse_list_fields(cls, value):
        """Convert comma-separated string to list if needed."""
        if isinstance(value, str):
            if not value.strip():
                return []
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("after_date")
    @classmethod
    def validate_date_format(cls, value):
        """Validate date format and convert to YYYY/MM/DD."""
        if not value:
            return value
        # Accept both YYYY/MM/DD and YYYY-MM-DD formats
        return value.replace("-", "/")


class GoogleDocsConfig(SourceConfig):
    """Google Docs configuration schema."""

    include_trashed: bool = Field(
        default=False,
        title="Include Trashed Documents",
        description="Include documents that have been moved to trash. Defaults to False.",
    )

    include_shared: bool = Field(
        default=True,
        title="Include Shared Documents",
        description="Include documents shared with you by others. Defaults to True.",
    )

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Sharing Permissions",
        description=(
            "Request each document's sharing permissions alongside its metadata so the "
            "document is retrievable by the same people who can open it in Google Docs. "
            "When false, permissions are left unknown and documents stay invisible until "
            "an administrator grants access at the project level."
        ),
    )


class GoogleDriveConfig(SourceConfig):
    """Google Drive configuration schema."""

    include_patterns: list[str] = Field(
        default=[],
        title="Include Patterns",
        description=(
            "List of file/folder paths to include in synchronization. "
            "Examples: 'my_folder/*', 'my_folder/my_file.pdf'. "
            "Separate multiple patterns with commas. If empty, all files are included."
        ),
    )

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Sharing Permissions",
        description=(
            "Request each file's sharing permissions alongside its metadata so the file "
            "is retrievable by the same people who can open it in Drive. When false, "
            "permissions are left unknown and documents stay invisible until an "
            "administrator grants access at the project level."
        ),
    )

    @field_validator("include_patterns", mode="before")
    @classmethod
    def _parse_include_patterns(cls, value):
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return value


class NotionConfig(SourceConfig):
    """Notion configuration schema."""

    pass


class OneDriveConfig(SourceConfig):
    """OneDrive configuration schema."""

    excluded_sensitivity_label_ids: list[str] = Field(
        default_factory=list,
        title="Excluded Sensitivity Labels",
        description=(
            "Microsoft Purview sensitivity label IDs (GUIDs). Files carrying any "
            "of these labels are skipped during sync. Sublabels must be listed "
            "explicitly. See docs for how to find label GUIDs in the Purview portal."
        ),
    )

    skip_encrypted_files: bool = Field(
        default=True,
        title="Skip Encrypted Files",
        description=(
            "Skip files protected with label-based encryption (Graph returns "
            "423 Locked). When false, encrypted files raise an error instead."
        ),
    )

    skip_unlabeled_files: bool = Field(
        default=False,
        title="Skip Unlabeled Files",
        description=(
            "Skip files that have no Purview sensitivity label applied. Useful "
            "for strict policies that only index explicitly classified content."
        ),
    )

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Item Permissions",
        description=(
            "Read each item's permissions from Microsoft Graph so the file is "
            "retrievable by the same people who can open it in OneDrive. Costs one "
            "extra API call per file. When false, permissions are left unknown and "
            "documents stay invisible until an administrator grants access at the "
            "project level."
        ),
    )


class OutlookCalendarConfig(SourceConfig):
    """Outlook Calendar configuration schema."""

    pass


class OutlookMailConfig(SourceConfig):
    """Outlook Mail configuration schema."""

    after_date: Optional[str] = Field(
        None,
        title="After Date",
        description="Sync emails after this date (format: YYYY/MM/DD or YYYY-MM-DD).",
    )

    included_folders: list[str] = Field(
        default=["inbox", "sentitems"],
        title="Included Folders",
        description=(
            "Well-known folder names to include (e.g., 'inbox', 'sentitems', 'drafts'). "
            "Defaults to inbox and sent items."
        ),
    )

    excluded_folders: list[str] = Field(
        default=["junkemail", "deleteditems"],
        title="Excluded Folders",
        description=(
            "Well-known folder names to exclude (e.g., 'junkemail', 'deleteditems'). "
            "Defaults to junk email and deleted items."
        ),
    )

    @field_validator("included_folders", "excluded_folders", mode="before")
    @classmethod
    def parse_list_fields(cls, value):
        """Convert comma-separated string to list if needed."""
        if isinstance(value, str):
            if not value.strip():
                return []
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("after_date")
    @classmethod
    def validate_date_format(cls, value):
        """Validate date format and convert to YYYY/MM/DD."""
        if not value:
            return value
        # Accept both YYYY/MM/DD and YYYY-MM-DD formats
        return value.replace("-", "/")


class OneNoteConfig(SourceConfig):
    """Microsoft OneNote configuration schema."""

    pass


class SlackConfig(SourceConfig):
    """Slack configuration schema."""

    include_private_channels: bool = Field(
        default=True,
        title="Include Private Channels",
        description=(
            "Sync private channels the app has been invited to. A private channel is "
            "indexed with its membership mirrored as access control, so only its members "
            "can retrieve the messages. Turn this off to index public channels only."
        ),
    )

    include_thread_replies: bool = Field(
        default=True,
        title="Include Thread Replies",
        description=(
            "Fetch each thread's replies and index them with the message that started "
            "the thread. When off, replies are not indexed at all — Slack's channel "
            "history returns only top-level messages."
        ),
    )

    max_messages_per_channel: int = Field(
        default=0,
        title="Max Messages Per Channel",
        description=(
            "Stop after this many messages in each channel. 0 means unlimited. Useful "
            "for a first, bounded run against a large workspace."
        ),
        ge=0,
    )


class TeamsConfig(SourceConfig):
    """Microsoft Teams configuration schema."""

    mirror_permissions: bool = Field(
        default=True,
        title="Mirror Channel Membership",
        description=(
            "Read who can see each channel or chat so its messages are retrievable by "
            "the same people who can read them in Teams. A standard channel is mirrored "
            "as its team's Entra group; a private channel and a chat are mirrored as "
            "their own members. Costs one extra API call per team, private channel and "
            "chat. When false, permissions are left unknown and messages stay invisible "
            "until an administrator grants access at the project level."
        ),
    )


class SharePointOnlineConfig(SourceConfig):
    """SharePoint Online configuration schema.

    Configures which SharePoint sites to sync and ACL behavior.
    """

    site_url: str = Field(
        default="",
        title="SharePoint Site URL",
        description=(
            "URL of a specific SharePoint site to sync "
            "(e.g., 'https://contoso.sharepoint.com/sites/Marketing'). "
            "Leave empty to sync all sites in the tenant."
        ),
    )

    @field_validator("site_url")
    @classmethod
    def validate_site_url_ssrf(cls, v: str) -> str:
        """Validate site URL for SSRF safety."""
        if not v:
            return v
        validate_url(v.strip())
        return v.strip()

    include_personal_sites: bool = Field(
        default=False,
        title="Include Personal Sites",
        description="Whether to include OneDrive personal sites in sync.",
    )

    include_pages: bool = Field(
        default=True,
        title="Include Site Pages",
        description="Whether to sync SharePoint site pages.",
    )

    excluded_sensitivity_label_ids: list[str] = Field(
        default_factory=list,
        title="Excluded Sensitivity Labels",
        description=(
            "Microsoft Purview sensitivity label IDs (GUIDs). Files carrying any "
            "of these labels are skipped during sync. The same list is matched "
            "against container labels on SharePoint sites and Teams; matching "
            "sites are skipped entirely. Sublabels must be listed explicitly. "
            "See docs for how to find label GUIDs in the Purview portal."
        ),
    )

    skip_encrypted_files: bool = Field(
        default=True,
        title="Skip Encrypted Files",
        description=(
            "Skip files protected with label-based encryption (Graph returns "
            "423 Locked). When false, encrypted files raise an error instead."
        ),
    )

    skip_unlabeled_files: bool = Field(
        default=False,
        title="Skip Unlabeled Files",
        description=(
            "Skip files that have no Purview sensitivity label applied. Useful "
            "for strict policies that only index explicitly classified content."
        ),
    )




class AuthConfig(BaseConfig):
    """Authentication config schema."""

    pass


class OAuth2AuthConfig(AuthConfig):
    """Base OAuth2 authentication config.

    This is for OAuth2 sources that only have access tokens (no refresh).
    These sources require going through the OAuth flow and cannot be created via API.
    """

    access_token: str = Field(
        title="Access Token",
        description="The access token for the OAuth2 app. This is obtained through the OAuth flow.",
    )


class OAuth2WithRefreshAuthConfig(OAuth2AuthConfig):
    """OAuth2 authentication config with refresh token support.

    These sources support refresh tokens for long-lived access.
    They require going through the OAuth flow and cannot be created via API.
    """

    refresh_token: str = Field(
        title="Refresh Token",
        description="The refresh token for the OAuth2 app. "
        "This is obtained through the OAuth flow.",
    )


class BoxAuthConfig(OAuth2WithRefreshAuthConfig):
    """Box authentication credentials schema."""
