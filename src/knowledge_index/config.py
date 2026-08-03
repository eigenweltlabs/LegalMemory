"""Validated product configuration; secrets are references, never inline values."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledge_index.taxonomies import PipelineStage

if TYPE_CHECKING:
    from knowledge_index.ontology import OntologyScope

# The two models a deployment runs, named by the environment that runs them. Nothing in
# this package names a model: an appliance that ships one has to be edited to change it.
DEFAULT_LLM_ENV = "KI_LLM_MODEL"
DEFAULT_EMBEDDING_ENV = "KI_EMBEDDING_MODEL"


# Model names are always the names the LiteLLM gateway serves them under. There is no
# intermediate layer: a stage (or a feature such as retrieval rerank) is assigned a
# gateway model directly, and every call goes to components.litellm_url. There is no
# offline mode either: a stage whose model is unreachable fails, retries, and
# quarantines — it never silently degrades.
def _default_llm() -> str:
    # Read from the environment rather than written here: the model a firm runs is a
    # property of its deployment, and a name compiled into the product is one that has
    # to be edited in a source file to change. Empty when unset, which the admin UI
    # shows as "Select a model…" and every caller fails loudly on — the alternative is
    # guessing at a model and billing for it.
    return os.environ.get(DEFAULT_LLM_ENV, "")


def _default_embedding_model() -> str:
    return os.environ.get(DEFAULT_EMBEDDING_ENV, "")


# What each stage's code currently does, as a version. Owned here and nowhere else: this
# is a property of the implementation, not a preference, and the pipeline reprocesses a
# document whenever the version it was last run under differs from this.
#
# classify_matter mvp-3: corpus search + local folder context. relate mvp-10: parked
# relation intents replayed after classify, on-request inline conversion of neighbours,
# honest version ordering (adjacent before/after insertion; 'unknown' stays a NULL
# ordinal with no supersedes edge), and tracked-changes evidence in the prompt — a
# revisions digest on the current file and on open_file results, with the bilateral
# redline_of/redline_by contract, layered email threading, list_folder(path) so
# sibling folders are explorable, defined relation kinds + emails-are-final in the
# prompt, and titles owned by the newest version. extract_metadata mvp-2: it stopped
# running only on final and executed versions and became the sole owner of document
# typing through the ontology walk.
CODE_STAGE_VERSIONS: dict[str, str] = {
    "classify_matter": "mvp-3",
    "relate": "mvp-10",
    "extract_metadata": "mvp-2",
}
DEFAULT_STAGE_VERSION = "mvp-1"


class StageConfig(BaseModel):
    enabled: bool = True
    # The gateway model this stage calls, by the name the LiteLLM gateway serves it
    # under. Read by the stages that talk to a model at all (classify_matter, relate,
    # extract_metadata, extract_decisions, gen_evals); fetch and convert move bytes,
    # and index embeds with retrieval.embedding_model because the query path must use
    # the very same model the vectors were written with.
    model: str = Field(default_factory=_default_llm)
    # The effective version, and never a setting. It is written by the validator below from
    # the code's own version plus whatever re-runs an operator has asked for, so a saved
    # config.json cannot hold it at a value the code has moved on from.
    #
    # It used to be a plain field, which meant an appliance that had ever saved its
    # configuration froze it: a stage whose behaviour changed in a release kept the old
    # version, requeue_outdated_stages never reached a single document already on disk, and
    # the change applied only to documents ingested afterwards. The mechanism whose entire
    # job is to say "the code changed, redo it" was silently disabled by a file written
    # months earlier.
    producer_version: str = DEFAULT_STAGE_VERSION
    # What an operator owns: pressing "Re-run" on the pipeline page bumps this, which
    # changes the effective version and requeues the stage and everything downstream. Kept
    # separate from the code's version precisely so the two cannot overwrite each other —
    # a re-run must survive a release, and a release must reach an appliance that has
    # re-run something.
    rerun_token: str = ""
    max_attempts: int = Field(default=3, ge=1, le=20)

    def effective_version(self, stage_name: str) -> str:
        base = CODE_STAGE_VERSIONS.get(stage_name, DEFAULT_STAGE_VERSION)
        return f"{base}+{self.rerun_token}" if self.rerun_token else base


def _default_stages() -> dict[str, StageConfig]:
    return {
        stage.value: StageConfig(
            enabled=stage != PipelineStage.GEN_EVALS,
            producer_version=CODE_STAGE_VERSIONS.get(stage.value, DEFAULT_STAGE_VERSION),
        )
        for stage in PipelineStage
    }


class OntologyConfig(BaseModel):
    """Which ontology artifact is plugged in and how it is scoped.

    The artifact is data (shipped or uploaded), never code. ``disabled_nodes``
    hides a node and its whole subtree; documents typed under a hidden node
    resolve to the nearest visible ancestor. Facets beyond ``doc_type`` ship
    dormant and activate here once a pipeline stage produces them."""

    artifact: str = "lmss"
    active_facets: list[str] = Field(
        default_factory=lambda: ["doc_type", "area_of_law", "service", "clause"]
    )
    disabled_nodes: list[str] = Field(default_factory=list)


class PipelineConfig(BaseModel):
    stages: dict[str, StageConfig] = Field(default_factory=_default_stages)
    max_file_mb: int = Field(default=512, ge=1)
    claim_timeout_seconds: int = Field(default=900, ge=10)
    retry_base_seconds: int = Field(default=5, ge=1)
    # The relate agent may pull a neighbour's fetch/convert forward through the normal
    # claim machinery when it needs to read a file that is not converted yet. The budget
    # bounds how long one open_file call spends on that (waiting on another worker's
    # conversion counts; a conversion this worker started runs to completion). The slots
    # cap how many inline conversions run concurrently per process, so the stage-level
    # concurrency plan for the docling service keeps meaning.
    inline_conversion_budget_seconds: int = Field(default=90, ge=1)
    inline_conversion_slots: int = Field(default=4, ge=1, le=64)
    # A sync that ends with new documents and no insertion is a dead end: the estate is
    # downloaded, nothing is searchable, and nothing tells the operator a second button
    # exists. On by default for that reason. Firms that want a partner to review the
    # scanned estate before paying for conversion and embedding turn it off, and the sync
    # run then completes with a null insertion_run_id.
    auto_insert_after_sync: bool = True
    # A deletion large enough to be indistinguishable from a connector failing to
    # enumerate is applied only after this many consecutive scans report the identical set
    # of missing objects. The documents stay searchable while it is being confirmed. 1
    # tombstones on the first scan that reports it — only for deployments that would
    # rather lose documents from the index than keep deleted ones in it.
    deletion_confirmations: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def include_every_stage(self) -> "PipelineConfig":
        for name, config in _default_stages().items():
            self.stages.setdefault(name, config)
        # Recomputed on every load, so whatever a saved config.json holds for
        # producer_version is discarded rather than obeyed. An operator's re-runs survive
        # because they live in rerun_token, which is theirs and is not touched here.
        for name, config in self.stages.items():
            config.producer_version = config.effective_version(name)
        return self

    def stage(self, name: str) -> StageConfig:
        return self.stages.get(name, _default_stages().get(name, StageConfig()))


class RetrievalConfig(BaseModel):
    index_name: str = "knowledge-index-chunks-v1"
    # One embedding model for the whole appliance: the index stage writes vectors with
    # it and every query embeds with it, so it lives here next to the dimensions that
    # complete the vector-space identity — not on a stage, and never per-call.
    embedding_model: str = Field(default_factory=_default_embedding_model)
    embedding_dimensions: int = Field(default=1536, ge=8, le=4096)
    # Vector search is APPROXIMATE (HNSW), never brute-force script_score. Lucene is the
    # default engine: it does native pre-filtered kNN (every leg here is ACL-filtered),
    # keeps the graph in the Lucene segment (no separate off-heap sizing), and ignores
    # ef_search (uses k), so there is one fewer knob. Reserve faiss for multi-million-
    # vector scale or when quantization is needed. space_type=cosinesimil suits Lucene;
    # note faiss has no native cosine (it auto-normalizes to inner product).
    vector_engine: str = Field(default="lucene", pattern="^(lucene|faiss|nmslib)$")
    vector_space_type: str = Field(default="cosinesimil")
    hnsw_m: int = Field(default=16, ge=2, le=100)
    hnsw_ef_construction: int = Field(default=128, ge=8, le=2000)
    chunk_chars: int = Field(default=1200, ge=200, le=10000)
    chunk_overlap_chars: int = Field(default=120, ge=0, le=2000)
    rerank_enabled: bool = False
    # The gateway model that scores the top collapsed hits when rerank_enabled is on.
    rerank_model: str = Field(default_factory=_default_llm)
    graph_rag_enabled: bool = False
    # Multi-leg fusion (RRF): every leg is ACL-scoped before fusion.
    # k is not a free knob — it is the gain control on how far a document can be
    # behind on relevance and still win on consensus. At k=60 a document appearing
    # in two legs beats a single-leg document 60 positions ahead of it, which lets
    # topically-similar-but-wrong documents outrank an exact lexical match; k=20
    # cuts that to 20 and measured +0.032 nDCG@10 on the 2026-08-03 benchmark.
    # Lower it further only together with the pool depth below.
    fusion_rrf_k: int = Field(default=20, ge=1, le=1000)
    weight_lexical: float = Field(default=1.0, ge=0)
    weight_semantic: float = Field(default=1.0, ge=0)
    weight_identifier: float = Field(default=1.5, ge=0)
    weight_decisions: float = Field(default=0.8, ge=0)
    # Legal authority decays by supersession, not by age — but supersession is a
    # property of a document's own version chain, so it is applied where collapse
    # chooses which version to surface, NOT as a cross-document score multiplier.
    # As a multiplier it cost 0.057 nDCG@10 on the 2026-08-03 benchmark: at
    # fusion_rrf_k=60 the old 1.2/0.7 range overrode 26 positions of the fused pool,
    # burying relevant drafts under irrelevant executed documents from other matters.
    # Kept as a deliberately gentle cross-document nudge (≤3 positions at k=20); set
    # all values to 1.0 to disable, or use SearchFilters.only_final for a hard rule.
    version_status_boost: dict[str, float] = Field(
        default_factory=lambda: {"executed": 1.05, "final": 1.0, "unknown": 0.98, "draft": 0.95}
    )
    collapse_per_document: bool = True
    max_chunks_per_document: int = Field(default=3, ge=1, le=20)
    # How many candidates each leg fetches, as a multiple of the requested result
    # limit. Fusion, the status boost, collapse and rerank all reorder — with a pool
    # the size of the answer they can only reshuffle what one leg already had in its
    # own top-N, and a document indexed as body+profile+clause spends three slots on
    # one document. 10x costs one OpenSearch page and buys the rankers room.
    candidate_pool_factor: int = Field(default=10, ge=1, le=50)
    # Boost applied to a chunk whose extracted parties match a name the query uses.
    # Extracted party metadata is otherwise reachable only through an exact filter
    # that callers almost never set, so it never reaches ranking. 0 disables.
    metadata_boost: float = Field(default=2.0, ge=0, le=10)
    # Restrict the fused search to these chunk kinds ("chunk" | "profile" | "clause");
    # None = all kinds. Query-time (the benchmark ablates profile/clause rows with it);
    # an explicit per-request SearchFilters.chunk_kind wins over this default.
    search_chunk_kinds: list[str] | None = None
    # signal-dense ingestion
    chunk_contextualize: bool = True
    profile_embeddings: bool = True
    clause_embeddings: bool = True


class EnvironmentConfig(BaseModel):
    """Caps that keep the RL-environment builder sparse and firm-specific."""

    max_per_practice_area: int = Field(default=8, ge=1, le=500)
    max_candidates_per_run: int = Field(default=50, ge=1, le=2000)
    min_confidence: float = Field(default=0.6, ge=0, le=1)


class SecurityConfig(BaseModel):
    unknown_acl_policy: str = Field(default="deny", pattern="^(deny|allow)$")
    auth_mode: str = Field(default="trusted_header", pattern="^(trusted_header|oidc)$")
    oidc_issuer: str = "http://keycloak:8080/realms/knowledge-index"
    oidc_audience: str = "knowledge-index"
    # An identity provider stamps `iss` with the hostname the *caller* used. An MCP
    # client runs on a lawyer's laptop and reaches the IdP on its public URL while this
    # appliance reaches it inside the container network, so the string that must match
    # `iss` and the URL the signing keys come from are two different things. Empty ->
    # derived from oidc_issuer, which is correct whenever both sides agree on one name.
    oidc_jwks_url: str = ""
    # RFC 8707 resource identifier for the MCP endpoint, advertised in the RFC 9728
    # protected-resource metadata and required in every MCP token's `aud`. Empty ->
    # derived from connectors.public_base_url, so one setting moves the whole appliance.
    mcp_resource: str = ""
    # Advertised in the protected-resource metadata as what a client should ask the
    # authorization server for. The last entry is what actually binds the token to this
    # appliance: no identity provider in wide use implements RFC 8707 resource
    # indicators yet (Keycloak 26 ignores ?resource= entirely), so the audience comes
    # from a scope whose mapper stamps the resource identifier into `aud`. Rename it to
    # whatever the firm's IdP calls that scope.
    # `openid` is deliberately absent. It is an OIDC keyword every client sends at the
    # authorization request anyway, not a client scope an IdP can grant — advertising it
    # here made clients register for it and Keycloak reject the registration outright
    # ("Requested scope 'openid' not trusted"). List only scopes the IdP actually holds.
    mcp_scopes: list[str] = Field(
        default_factory=lambda: ["profile", "email", "knowledge-index-mcp"]
    )
    # Development escape hatch: let `x-ki-principals` authenticate MCP calls. Anyone who
    # can reach the port then becomes any lawyer in the firm, so it is off by default and
    # has to be turned on deliberately. Never on a firm's appliance.
    mcp_allow_trusted_header: bool = False
    subject_claim: str = "sub"
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    admin_groups: list[str] = Field(default_factory=lambda: ["knowledge-index-admins"])
    trusted_header_name: str = "x-ki-principals"
    trusted_header_secret: str | None = None
    # How a mirrored source ACL combines with local project/document grants for
    # externally hosted sources. "sufficient" mirrors the source faithfully;
    # "intersect" additionally requires a local grant, so an over-broad share at the
    # source cannot reach past a matter restriction. See permissions.AccessService.
    source_acl_mode: str = Field(default="sufficient", pattern="^(sufficient|intersect)$")
    # Explicit bridges between source principals and this appliance's identities, for
    # sources that cannot report group memberships:
    #   {"group:entra:2b1f…": "group:litigation"}
    principal_aliases: dict[str, str] = Field(default_factory=dict)
    # Force a full rescan at least this often even when a delta feed is available: a
    # permission change at source alters no document's etag, so only a full scan
    # re-reads ACLs and notices a revocation.
    acl_refresh_hours: int = 24

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url.strip() or (
            f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"
        )


class ConnectorsConfig(BaseModel):
    """Settings for the in-process connector layer.

    Connectors run inside the app and worker; there is no connector service to point
    at. What remains configurable is where a browser comes back to after an OAuth
    handshake, and whether a connector may reach the firm's own network.
    """

    # Public base URL of this appliance. The OAuth redirect URI is derived from it and
    # must match what the firm registered in Entra ID / Google Cloud exactly.
    public_base_url: str = "http://localhost:8000"
    oauth_callback_path: str = "/api/connectors/oauth/callback"
    # Only for sources genuinely hosted on the firm's LAN. Off by default: the SSRF
    # guard is what stops a connector being steered at the appliance's own services.
    allow_private_hosts: bool = False
    events: "ConnectorEventsConfig" = Field(default_factory=lambda: ConnectorEventsConfig())

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.oauth_callback_path}"


class GoogleDriveEventsConfig(BaseModel):
    """Outbound-only transport for Google Workspace Drive events.

    The OAuth user creates and renews Workspace subscriptions; a service account only
    consumes the Pub/Sub pull subscription. The service-account JSON is deliberately
    referenced through an environment variable or mounted file and never enters
    config.json or the admin API.
    """

    topic: str = ""
    pull_subscription: str = ""
    service_account_file: str = ""
    service_account_json_env: str = "KI_GOOGLE_EVENTS_SERVICE_ACCOUNT_JSON"

    @property
    def configured(self) -> bool:
        return bool(
            self.topic.strip()
            and self.pull_subscription.strip()
            and (
                self.service_account_file.strip()
                or os.environ.get(self.service_account_json_env, "").strip()
            )
        )


class MicrosoftGraphEventsConfig(BaseModel):
    """Outbound-only Azure Event Hubs transport for Microsoft Graph notifications."""

    # The EventHub: URL Microsoft Graph writes to. This is not the AMQP address the
    # appliance consumes from; keeping them separate prevents a portal value being pasted
    # into the wrong field and producing a subscription that can never be received.
    notification_url: str = ""
    fully_qualified_namespace: str = ""
    event_hub_name: str = ""
    consumer_group: str = "$Default"
    tenant_id: str = ""
    client_id: str = ""
    client_secret_env: str = "KI_MICROSOFT_EVENTS_CLIENT_SECRET"

    @property
    def coordinates_configured(self) -> bool:
        """Whether every non-secret Event Hubs coordinate is present."""

        return bool(
            self.notification_url.strip()
            and self.fully_qualified_namespace.strip()
            and self.event_hub_name.strip()
            and self.tenant_id.strip()
            and self.client_id.strip()
        )

    @property
    def configured(self) -> bool:
        """Whether a dedicated deployment secret completes the configuration.

        The SharePoint adapter can additionally reuse a matching connector credential
        from the encrypted credential store. Keeping that database-aware fallback out
        of the Pydantic settings object avoids making configuration parsing open a
        database connection.
        """

        return bool(
            self.coordinates_configured
            and os.environ.get(self.client_secret_env, "").strip()
        )


class ConnectorEventsConfig(BaseModel):
    """Provider-event delivery shared by every connector adapter."""

    enabled: bool = True
    reconcile_seconds: int = Field(default=300, ge=30, le=3600)
    google_drive: GoogleDriveEventsConfig = Field(default_factory=GoogleDriveEventsConfig)
    microsoft_graph: MicrosoftGraphEventsConfig = Field(
        default_factory=MicrosoftGraphEventsConfig
    )


class IdentityConfig(BaseModel):
    """Where the realm lives, so sign-in is set up here instead of in Keycloak's console.

    An administrator who has to open a second admin UI on another port with another
    password has two products to keep correct. These settings let this appliance drive
    the realm over Keycloak's admin REST API on their behalf.
    """

    # Two names for one server, and both are load-bearing. The appliance talks to
    # Keycloak inside the container network; the browser and the identity provider
    # reach it on its published name, and the broker redirect URI a firm registers at
    # Google or Entra is derived from that public name.
    admin_base_url: str = "http://keycloak:8080"
    public_base_url: str = "http://localhost:8083"
    realm: str = "knowledge-index"
    admin_realm: str = "master"
    admin_client_id: str = "admin-cli"
    # The client whose tokens the appliance validates: the audience mapper goes here.
    audience_client_id: str = "knowledge-index-ui"
    # Every client that mints a token for a person, and so needs `sub` and full
    # (non-lightweight) access tokens.
    token_client_ids: list[str] = Field(
        default_factory=lambda: ["knowledge-index-ui", "knowledge-index-mcp"]
    )
    # Credentials are read from the environment at call time, never held in this file:
    # a realm administrator password is exactly as sensitive as a connector secret.
    admin_username_env: str = "KI_KEYCLOAK_ADMIN_USERNAME"
    admin_password_env: str = "KI_KEYCLOAK_ADMIN_PASSWORD"

    def broker_redirect_uri(self, alias: str) -> str:
        """The URI a firm registers at its provider. Wrong here means no login at all."""
        return f"{self.public_base_url.rstrip('/')}/realms/{self.realm}/broker/{alias}/endpoint"


class BackupDestinationConfig(BaseModel):
    """Where full-appliance backups are written.

    Two kinds, because those are the two a firm actually has. ``local`` is a directory —
    in practice a mounted NAS, SMB share or an external disk, which is how most firms of
    this size hold their off-machine copy. ``s3`` is any S3-compatible endpoint (MinIO on
    the firm's own hardware, Wasabi, AWS), which is what gives the 3-2-1 rule its
    off-site leg.

    No credential appears here. ``config.json`` is written in the clear, so the backup key
    and the object-storage keys live in the database encrypted at rest — set under Backup
    in the admin UI, or with ``ki backup-key``.
    """

    kind: str = Field(default="local", pattern="^(local|s3|restic)$")
    # local: the directory backups are written under. Must be a mount that survives the
    # container — writing backups into the container's own filesystem protects nothing.
    path: str = "/backups"
    # s3-compatible
    bucket: str = ""
    endpoint_url: str = ""  # empty -> AWS's own endpoint for the region
    region: str = "us-east-1"
    # Path-style addressing by default: MinIO and most on-prem gateways serve it, and
    # virtual-host style needs wildcard DNS the firm may not have.
    use_path_style: bool = True
    # Common to all kinds: every backup lands under <prefix>/<backup id>/, and for restic
    # the prefix is the repository directory or key prefix.
    prefix: str = "knowledge-index"

    # ------------------------------------------------------------------------- restic
    # A restic repository, which is the only kind of destination here that stores a night
    # as the difference from the night before. `local` and `s3` store whole objects: a
    # hundred thousand documents transferred and stored again every night, times whatever
    # retention keeps. restic stores content-defined chunks, so an unchanged blob store
    # costs nothing the second time, and it encrypts and verifies what it stores itself.
    #
    # Empty means "derive it": <path>/<prefix> for a directory, or
    # s3:<endpoint>/<bucket>/<prefix> when a bucket is set. Set it explicitly for the
    # backends restic supports and this appliance does not model — sftp:, rest:, azure:.
    restic_repository: str = ""
    # Snapshots are attributed to the appliance rather than to whichever container
    # happened to run the backup, so restic's own grouping stays stable across restarts.
    restic_host: str = "knowledge-index"
    # restic's cache is per-repository local state. A container that is recreated nightly
    # rebuilds it every run for no benefit, and on a read-only root filesystem it fails.
    restic_no_cache: bool = False


class BackupRetentionConfig(BaseModel):
    """Grandfather-father-son retention over full backups.

    A backup is kept if it is one of the newest ``daily``, or the newest in one of the
    last ``weekly`` ISO weeks, or the newest in one of the last ``monthly`` months, or the
    newest in one of the last ``yearly`` years. Everything else is pruned. Counting
    distinct periods rather than days means a stack that was switched off for a fortnight
    still keeps a year of history instead of ageing it out on the calendar.
    """

    daily: int = Field(default=7, ge=0, le=365)
    weekly: int = Field(default=4, ge=0, le=520)
    monthly: int = Field(default=6, ge=0, le=120)
    yearly: int = Field(default=2, ge=0, le=50)
    # Floor under every other rule. Retention that can empty the destination is a delete
    # feature wearing a backup feature's clothes; the newest backup is never pruned.
    min_keep: int = Field(default=1, ge=1, le=100)
    # Pruning is what makes the schedule sustainable, but it also deletes a firm's only
    # off-machine copy if the rules are wrong, so it is opt-in and reported before it runs.
    prune_enabled: bool = True


class BackupSourcesConfig(BaseModel):
    """Which stores a backup captures. Everything true here is captured every run.

    The defaults are "everything that cannot be rebuilt, plus the index that is merely
    expensive to rebuild". Turning one off is a deliberate statement that the firm accepts
    losing it, so each flag says what is lost.
    """

    # The ontology, ACLs, audit ledger, encrypted connector credentials — the appliance.
    # Not a toggle: a backup without it is not a backup, so there is no flag for it.
    # LiteLLM's spend ledger and runtime model registry, and Langfuse's model-call traces.
    # Both are audit records a firm may be required to produce.
    gateway_databases: bool = True
    # Hatchet's own database: workflow definitions, run history, the durable queue.
    orchestrator_database: bool = True
    # The OpenSearch index. Rebuildable from Postgres by re-embedding every chunk, which
    # costs real money and hours — so it is captured by default and can be dropped by
    # firms that would rather pay for the rebuild than store the vectors twice.
    search_index: bool = True
    # Content-addressed originals of every fetched document. Re-fetchable only while the
    # source still exists and still holds the file, which is exactly what a restore cannot
    # assume.
    artifact_blobs: bool = True
    # Files uploaded through the admin UI. There is no upstream copy anywhere; losing this
    # loses the documents outright.
    uploaded_files: bool = True
    # Mid-sync scratch. Off by default: it is meaningful only to a scan that is already
    # running, and a restore starts no scan.
    connector_staging: bool = False
    # Keycloak's data volume — users, sessions, client secrets, realm signing keys.
    identity_volume: bool = True
    # Hatchet's generated server config, which is what makes an existing client token valid.
    orchestrator_config_volume: bool = True
    # The KI_* deployment secrets, above all KI_CONNECTOR_CREDENTIAL_KEY. Without that key
    # the restored source_credentials rows are undecryptable ciphertext and every connector
    # has to be re-authorized by hand. Requires encryption to be on — see BackupConfig.
    environment_secrets: bool = True
    # Folders the watcher is told to keep an eye on. Small, and the only record of which
    # folders a firm asked to be watched — a restore without them leaves the watcher idle
    # over an estate nobody notices has stopped being indexed.
    watched_folders: bool = True
    # Anything else this deployment keeps on disk, absolute paths inside the container.
    extra_paths: list[str] = Field(default_factory=list)


class BackupScheduleConfig(BaseModel):
    """A daily wall-clock schedule, evaluated in UTC.

    Not an interval: an operator asks for "every night at two", and an interval drifts
    across the working day the moment a run is slow or the appliance restarts. Due-ness is
    read from the run ledger, so a missed night is picked up on the next tick rather than
    skipped, and a failed backup does not re-fire in a loop.
    """

    enabled: bool = False
    # Read in ``timezone`` below, not in UTC.
    hour: int = Field(default=2, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    # An IANA name, because "every night at two" means two o'clock where the firm is, and
    # a UTC schedule moves an hour twice a year against it. A backup window that walks
    # into the working day each spring is a backup window nobody chose.
    timezone: str = "UTC"
    # A backup that starts while a sync or an insertion is mid-flight captures a database
    # that is consistent (pg_dump is MVCC) but an artifact directory that is not: a blob
    # can be half-written, and a source_objects row can point at a staged file the tar
    # missed. Waiting is almost always what an operator wants overnight.
    defer_while_active: bool = True
    # How long to keep deferring before giving up for the night and reporting it. An
    # appliance that is never idle would otherwise silently never be backed up.
    defer_limit_minutes: int = Field(default=180, ge=0, le=1440)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        """Refuse a timezone this machine cannot resolve, at save time.

        A name the system has no rules for would otherwise be accepted by the admin UI and
        then quietly fall back to UTC every night, which is the class of failure this
        whole feature exists to avoid.
        """
        name = (value or "UTC").strip() or "UTC"
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{name!r} is not a timezone this system knows. Use an IANA name such as "
                "'Europe/Berlin' or 'UTC'."
            ) from exc
        return name


class BackupConfig(BaseModel):
    """Full-appliance backup: every store, one archive, one manifest.

    The unit is the whole appliance, not a database. Restoring a firm's Postgres dump
    without the OpenSearch index, the uploaded files and the connector credential key
    produces something that starts, answers, and is quietly missing a third of the estate
    — which is worse than a restore that refuses to run. So a backup is a set of
    components captured together, checksummed together, and described by one manifest that
    a restore refuses to proceed without.
    """

    enabled: bool = False
    destination: BackupDestinationConfig = Field(default_factory=BackupDestinationConfig)
    sources: BackupSourcesConfig = Field(default_factory=BackupSourcesConfig)
    retention: BackupRetentionConfig = Field(default_factory=BackupRetentionConfig)
    schedule: BackupScheduleConfig = Field(default_factory=BackupScheduleConfig)
    # On by default, and required before deployment secrets may be captured. A full backup
    # of this appliance contains privileged client documents and the keys to the firm's
    # document estate; it travels to a NAS or a bucket that is not covered by the
    # appliance's own access control, so it is encrypted before it leaves.
    encrypt: bool = True
    # Read every component back from the destination after writing and re-check its
    # checksum. Doubles the transfer, and is the only thing that distinguishes a backup
    # that exists from a backup that is readable. "Zero unverified restores" starts here.
    verify_after_write: bool = True
    # Refuse to start while documents are mid-pipeline, the way the snapshot scripts do.
    # A manual backup may override it; the schedule waits instead (see defer_while_active).
    require_settled_pipeline: bool = True
    # Guardrail on a single component, not on the total: a runaway tar is the failure mode
    # worth catching, and a firm with a genuinely large estate should raise this
    # deliberately rather than discover a silently truncated archive at restore time.
    max_component_gb: int = Field(default=512, ge=1, le=10000)

    @property
    def encryption_is_guaranteed(self) -> bool:
        """Whether what leaves this appliance is encrypted, whoever does the encrypting.

        A restic repository encrypts and authenticates every chunk under its own password,
        so ``encrypt`` is not merely unnecessary there — it has to stay off, because
        sealing a component first would hand restic high-entropy ciphertext that shares no
        chunks with anything and deduplication would stop working entirely.
        """
        return self.encrypt or self.destination.kind == "restic"

    @model_validator(mode="after")
    def secrets_require_encryption(self) -> "BackupConfig":
        """Deployment secrets may only be captured into an encrypted backup.

        Writing KI_CONNECTOR_CREDENTIAL_KEY as plaintext onto a NAS hands over every
        connector refresh token to anyone who can read the share, which is a larger hole
        than the one the backup closes. Refused at validation time so it is impossible to
        save from the admin UI, not merely discouraged in the documentation."""
        if self.sources.environment_secrets and not self.encryption_is_guaranteed:
            raise ValueError(
                "backup.sources.environment_secrets requires backup.encrypt: deployment "
                "secrets are never written to a backup in the clear. Turn on encryption "
                "and set the key in the environment, or turn off environment_secrets and "
                "keep those secrets somewhere else."
            )
        return self


class ComponentsConfig(BaseModel):
    litellm_url: str = "http://litellm:4000"
    docling_url: str = "http://docling:5001"
    opensearch_url: str = "http://opensearch:9200"
    orchestrator_provider: str = "hatchet"
    orchestrator_api_url: str = ""
    orchestrator_ui_url: str = ""
    # Two URLs for one service, like the orchestrator above: the appliance probes Langfuse
    # over the container network, a browser opens it on the host. Using the public URL for
    # both made the health check resolve to the app container itself and report a running
    # Langfuse as unreachable.
    traces_api_url: str = "http://langfuse:3000"
    traces_url: str = "http://localhost:3001"
    # Base URL of the hosted product documentation. Browser-facing only: the UI links
    # to it from the sidebar and the connector setup panels. Empty hides those links.
    docs_url: str = ""


def _model_slug(model: str) -> str:
    """Filesystem/index-safe slug of a model id (no regex, per the project mandate)."""
    slug = "".join(char if char.isalnum() else "-" for char in model.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "model"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KI_", env_nested_delimiter="__")

    artifact_dir: Path = Path(".ki/artifacts")
    # The gateway model the reference /api/ask assistant plans and answers with.
    ask_model: str = Field(default_factory=_default_llm)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    environments: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    components: ComponentsConfig = Field(default_factory=ComponentsConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    ontology: OntologyConfig = Field(default_factory=OntologyConfig)

    def ontology_uploads_dir(self) -> Path:
        return self.artifact_dir / "ontologies"

    def ontology_facet(self, facet: str) -> "OntologyScope":
        """The scoped view of ONE facet of the active ontology.

        Every consumer names its facet explicitly — the document-typing agent
        must never see Area of Law roots and vice versa. Resolve at task
        execution time (config is re-read per task) so a mid-run artifact or
        scope change applies to every not-yet-processed document; the call
        itself is cached on (artifact file, facet, scope)."""
        from knowledge_index.ontology import discover_artifacts, ontology_scope

        artifacts = discover_artifacts(self.ontology_uploads_dir())
        path = artifacts.get(self.ontology.artifact)
        if path is None:
            raise ValueError(
                f"ontology artifact {self.ontology.artifact!r} not found; "
                f"available: {sorted(artifacts)}"
            )
        return ontology_scope(path, (facet,), self.ontology.disabled_nodes)

    def doc_ontology(self) -> "OntologyScope":
        """The document-typing facet (see ontology_facet)."""
        return self.ontology_facet("doc_type")

    def browse_ontology(self) -> "OntologyScope":
        """All ACTIVE facets in one scope — for the dashboard tree editor and
        cross-facet search, never for a pipeline producer."""
        from knowledge_index.ontology import discover_artifacts, ontology_scope

        artifacts = discover_artifacts(self.ontology_uploads_dir())
        path = artifacts.get(self.ontology.artifact)
        if path is None:
            raise ValueError(
                f"ontology artifact {self.ontology.artifact!r} not found; "
                f"available: {sorted(artifacts)}"
            )
        return ontology_scope(
            path, self.ontology.active_facets, self.ontology.disabled_nodes
        )

    def embedding_signature(self) -> str:
        """Identity of the vectors an index holds: embedding model + dimension.

        Two vectors may share one ANN index only if they share this signature."""
        return f"{_model_slug(self.retrieval.embedding_model)}-{self.retrieval.embedding_dimensions}"

    def derived_index_name(self) -> str:
        """Canonical chunk-index name bound to the embedding signature, so switching
        the embedding model or dimension always targets a fresh, uniform index."""
        return f"knowledge-index-chunks-{self.embedding_signature()}"
