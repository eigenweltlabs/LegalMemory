"""Unified on-prem administration, data, authorization, retrieval, and MCP application."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.auth import Identity, IdentityResolver
from knowledge_index.connectors.runtime.secrets import (
    CredentialCryptoError,
    decrypt_credentials,
    encrypt_credentials,
    key_fingerprint,
)
from knowledge_index import artifacts
from knowledge_index.artifacts import LocalArtifactStore, ReclaimReport
from knowledge_index.config import AppConfig
from knowledge_index.config_store import ConfigStore, EnvironmentPinnedSetting
from knowledge_index.db import get_engine, init_db
from knowledge_index.db.models import (
    Artifact,
    Extraction,
    AuditEvent,
    BillingInvoice,
    BillingLineItem,
    Blob,
    Chunk,
    DecisionRecord,
    Document,
    DocumentGrant,
    DocumentVersion,
    DocumentVersionSource,
    EvalRecord,
    ExternalClient,
    IdentityProviderCredential,
    Matter,
    MatterAssignment,
    PipelineRun as PipelineRunRecord,
    ProcessingState,
    Project,
    ProjectGrant,
    Source,
    SourceCredential,
    SourceGroupMember,
    SourceObject,
    SourceObjectGrant,
    UsageEvent,
)
from knowledge_index.downloads import DownloadTokenStore
from knowledge_index.graph import GraphService
from knowledge_index import identity_admin
from knowledge_index.mcp_auth import (
    bearer_challenge,
    presented_bearer_token,
    protected_resource_metadata,
    resolve_mcp_identity,
)
from knowledge_index.mcp_server import create_mcp_server
from knowledge_index.permissions import AccessService, configure_access
from knowledge_index.pipeline import PipelineRunner
from knowledge_index.pipeline.providers import (
    chat_json,
    gateway_admin_headers,
    gateway_url,
    usage_stage,
)
from knowledge_index.pipeline.runner import connector_from_source
from knowledge_index.ontology import OntologyArtifact, discover_artifacts, ontology_scope
from knowledge_index.retrieval import RetrievalService, SearchFilters
from knowledge_index.sync import runs as sync_runs
from knowledge_index.taxonomies import (
    DISABLED_BY_CONFIGURATION,
    STAGE_BUCKET_DISABLED,
    STAGE_BUCKET_WAITING,
    WAITING_FOR_PREVIOUS_STAGE,
    PipelineStage,
    ProcessingStatus,
)


class SyncRequest(BaseModel):
    """Optional body of ``POST /api/actions/sync``: one source, or all of them."""

    source_id: str | None = None


class BackupRequest(BaseModel):
    """Optional body of ``POST /api/actions/backup``."""

    # Take the backup even though documents are mid-pipeline. An operator about to take
    # the appliance down wants a slightly ragged backup more than a refusal, but it has
    # to be asked for so the nightly schedule cannot do it quietly.
    force: bool = False


class BackupIdRequest(BaseModel):
    backup_id: str = Field(min_length=1, max_length=255)
    # Where to read it from. Empty means the configured destination.
    source_path: str = ""


class BackupFolderRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)


class BackupSecretRequest(BaseModel):
    """One secret, typed into the admin UI rather than referenced from a compose file."""

    name: str = Field(min_length=1, max_length=50)
    # Absent means "forget this one". Never echoed back by any endpoint.
    value: str | None = Field(default=None, max_length=4096)


class RestoreRequest(BaseModel):
    """What to restore, from where, and which stores to actually write back."""

    backup_id: str = Field(min_length=1, max_length=255)
    # Empty means the configured destination. A recovery onto fresh hardware starts with a
    # drive mounted somewhere this appliance has never heard of, so it can be told where.
    source_path: str = ""
    # Nothing is applied unless asked for. Staging alone is safe on a live appliance and is
    # how a firm establishes that its backups are restorable before it needs them to be.
    apply_databases: bool = False
    apply_files: bool = False
    apply_search_index: bool = False
    # Keycloak's users and the orchestrator's config. Replaced through the restore agent,
    # which is the only thing here that can stop the containers owning them.
    apply_volumes: bool = False


class BackupPruneRequest(BaseModel):
    # Defaults to reporting rather than deleting: this is the one backup operation that
    # destroys a firm's off-machine copies, so the safe answer is the one you get by
    # forgetting to say which you meant.
    dry_run: bool = True


class SourceCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    kind: str = "local_fs"
    root: Path | None = None
    project_id: str | None = None
    provider: str = "native"
    provider_connection_id: str | None = None
    config: dict | None = None  # connector config entered directly in the admin UI
    # Bring-your-own OAuth client: the firm registers its own app and supplies these.
    # Stored encrypted, never in Source.config.
    client_id: str | None = None
    client_secret: str | None = None
    # Non-OAuth connectors (PAT, app password) supply a token instead.
    access_token: str | None = None
    # Required to grant a mailbox or personal drive to a group or role: doing so
    # publishes one person's correspondence to everyone in that group.
    confirm_broad_grant: bool = False
    default_acl: list[dict] | None = None
    acl_by_path: dict[str, list[dict]] | None = None
    sync_policy: dict = Field(default_factory=lambda: {"mode": "continuous", "interval": "5m"})


class OntologyScopeUpdate(BaseModel):
    artifact: str | None = None
    active_facets: list[str] | None = None
    disabled_nodes: list[str] | None = None


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=2000)
    project_id: str | None = None
    matter_id: str | None = None
    doc_type: str | None = None
    version_status: str | None = None
    language: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ProjectCreate(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    initial_principal: str | None = None


class GrantCreate(BaseModel):
    principal: str = Field(min_length=3, max_length=512)
    principal_kind: str = Field(default="group", pattern="^(user|group|service|role)$")
    effect: str = Field(default="allow", pattern="^(allow|deny)$")
    role: str = Field(default="viewer", pattern="^(viewer|editor|admin|owner)$")


class ExternalClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    kind: str = Field(default="mcp", pattern="^(mcp|api)$")
    principal: str = Field(min_length=3, max_length=512)
    secret_ref: str | None = None
    allowed_project_ids: list[str] = Field(default_factory=list)


class SignInProviderCreate(BaseModel):
    """How the firm's people sign in, as an administrator supplies it.

    ``client_secret`` is write-only: it goes to Keycloak and to an encrypted row, and
    no endpoint on this appliance ever returns it.
    """

    kind: Literal["google", "entra", "okta", "oidc"]
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=2048)
    # Tenant id for Entra, Okta domain, discovery URL for anything else. Unused for
    # Google, whose discovery document is at one address for every customer.
    extra: str = Field(default="", max_length=1000)
    display_name: str = Field(default="", max_length=120)


class PrincipalAliasCreate(BaseModel):
    """Bridge one sign-in identity onto the identity a source reported for the person."""

    principal: str = Field(min_length=3, max_length=512)
    alias: str = Field(min_length=3, max_length=512)


class LocalPersonCreate(BaseModel):
    """Someone who signs in with a password here, for a firm with no directory.

    Only the address is required, and it is the only field that matters: it becomes
    the realm username, and it is what every connector's mirrored membership is
    matched against.
    """

    email: str = Field(min_length=3, max_length=320)
    first_name: str = Field(default="", max_length=120)
    last_name: str = Field(default="", max_length=120)


class PersonEnabledUpdate(BaseModel):
    enabled: bool


class EnvironmentUpdate(BaseModel):
    """Admin edits to an RL-environment candidate before (or after) approval."""

    instruction: str | None = None
    task_type: str | None = None
    practice_area: str | None = None
    holdout: bool | None = None
    review_note: str | None = None
    rubric: list[dict] | None = None
    verifiers: list[dict] | None = None


class ModelRegistration(BaseModel):
    """A model an administrator adds to the gateway at runtime.

    ``credential_name`` names one of the gateway's configured provider credentials
    (deploy/litellm/config.yaml). Raw provider keys are deliberately not accepted:
    they belong in the gateway container's environment, never in a browser form,
    a request body, or this appliance's database."""

    model_name: str = Field(min_length=1, max_length=150)  # the alias assignments point at
    model: str = Field(min_length=1, max_length=200)  # upstream id, e.g. openai/gpt-4o-mini
    credential_name: str | None = Field(default=None, max_length=150)
    api_base: str | None = Field(default=None, max_length=500)
    mode: Literal["chat", "embedding", "rerank"] = "chat"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    project_id: str | None = None


class PlanFilters(BaseModel):
    """Retrieval filters the planner may set. Mirrors the SearchFilters columns
    the caller is allowed to constrain; project_id is overridden by the request."""

    project_id: str | None = None
    matter_id: str | None = None
    doc_type: str | None = None
    version_status: str | None = None
    language: str | None = None


class PlanStep(BaseModel):
    """One retrieval call the executor should run. `tool` picks the
    RetrievalService method; the remaining fields are its arguments."""

    tool: str = Field(pattern="^(search_semantic|search_filter|search_decisions|traverse)$")
    query: str | None = None
    filters: PlanFilters = Field(default_factory=PlanFilters)
    entity_type: str | None = None
    entity_id: str | None = None


class Plan(BaseModel):
    """The planner's chosen sequence of ACL-scoped retrieval calls."""

    steps: list[PlanStep] = Field(default_factory=list, max_length=6)


class Citation(BaseModel):
    document_id: str
    title: str | None = None
    quote: str = ""


class AskAnswer(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)


_log = logging.getLogger(__name__)


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    config_store: ConfigStore | None = None,
) -> FastAPI:
    if session_factory is None:
        init_db()
        session_factory = sessionmaker(get_engine(), expire_on_commit=False)
    if config_store is None:
        config_store = ConfigStore(Path(".ki/config.json"))

    # Install the access rules before any request can be served, so no endpoint can
    # answer with a different permission model than the rest of the appliance.
    _security = config_store.get().security
    configure_access(
        source_acl_mode=_security.source_acl_mode,
        principal_aliases=_security.principal_aliases,
    )

    download_tokens = DownloadTokenStore(ttl_seconds=300)
    mcp = create_mcp_server(session_factory, config_store.get, download_tokens)
    mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
    app = FastAPI(
        title="Knowledge Index",
        description="On-prem connector, insertion, and permission-scoped retrieval plane.",
        version="0.2.0",
        lifespan=mcp_app.lifespan,
    )
    app.state.session_factory = session_factory
    app.state.config_store = config_store

    @app.exception_handler(CredentialCryptoError)
    async def _credential_crypto_error(request: Request, exc: CredentialCryptoError):
        """Report a missing or unusable credential key as configuration, not a crash.

        Connecting an OAuth source on a deployment without
        ``KI_CONNECTOR_CREDENTIAL_KEY`` is a setup step left undone, not a failure of
        the request — 503 says so, and refusing to store the credential in the clear is
        the correct outcome either way. The exception text names the environment
        variable and the command to generate it, so it goes to the log; the response
        stays generic because the OAuth callback shares this handler and is
        unauthenticated by necessity.
        """
        _log.error("connector credential storage is unavailable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "connector credential storage is not configured on this deployment. "
                    "See the appliance log for the remedy."
                )
            },
        )

    def resolve_identity(request: Request, *, admin: bool = False) -> Identity:
        try:
            identity = IdentityResolver(config_store.get().security).resolve(request.headers)
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if admin and not identity.is_admin:
            raise HTTPException(status_code=403, detail="administrator permission is required")
        return identity

    @app.middleware("http")
    async def audit_api_request(request: Request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        started = time.perf_counter()
        principals: list[str] = []
        try:
            principals = sorted(
                IdentityResolver(config_store.get().security).resolve(request.headers).principals
            )
        except PermissionError:
            pass
        outcome = "success"
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            outcome = (
                "success"
                if status_code < 400
                else "denied"
                if status_code in {401, 403}
                else "error"
            )
        except Exception:
            outcome = "error"
            raise
        finally:
            principals = getattr(request.state, "audit_principals", principals)
            with session_factory() as audit_session:
                audit_session.add(
                    AuditEvent(
                        actor_principals=principals,
                        action=f"api.{request.method.lower()}.{request.url.path.removeprefix('/api/')}",
                        target_type="api_route",
                        target_id=request.url.path,
                        outcome=outcome,
                        details={
                            "status_code": status_code,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        },
                    )
                )
                audit_session.commit()
        return response

    @app.middleware("http")
    async def challenge_unauthenticated_mcp(request: Request, call_next):
        """Answer an unauthenticated MCP request with the challenge that starts a login.

        An MCP client discovers where to sign in from this 401 alone (RFC 9728 §5.1).
        Without the header it has nothing to go on and simply reports a failure to the
        lawyer, which is why the check lives here rather than inside the tools: the
        transport must refuse before the JSON-RPC layer answers with a 200 that carries
        an error nobody can act on.

        Enforcing here as well as in every tool is deliberate. This gate is what a
        client needs to see; the tool-level check is what makes the identity behind a
        result true. Neither is redundant with the other, and the token validation
        itself is a signature check over a cached JWKS.
        """

        path = request.url.path
        if path != "/mcp" and not path.startswith("/mcp/"):
            return await call_next(request)
        # A CORS preflight carries no Authorization header by definition; challenging it
        # would stop a browser-based client before it ever sends the real request.
        if request.method == "OPTIONS":
            return await call_next(request)
        config = config_store.get()
        try:
            resolve_mcp_identity(request.headers, config)
        except PermissionError as exc:
            rejected = presented_bearer_token(request.headers)
            if rejected:
                # Somebody presented credentials that did not hold up. Unlike the
                # tokenless first step of the handshake, that is worth a ledger entry.
                with session_factory() as audit_session:
                    audit_session.add(
                        AuditEvent(
                            actor_principals=[],
                            action="mcp.authenticate",
                            target_type="api_route",
                            target_id=path,
                            outcome="denied",
                            details={"reason": str(exc)},
                        )
                    )
                    audit_session.commit()
            return JSONResponse(
                status_code=401,
                content={
                    "error": "invalid_token" if rejected else "unauthorized",
                    "error_description": str(exc),
                },
                headers={
                    "WWW-Authenticate": bearer_challenge(
                        config,
                        error="invalid_token" if rejected else "",
                        description=str(exc) if rejected else "",
                    )
                },
            )
        return await call_next(request)

    static_root = files("knowledge_index.web").joinpath("static")
    app.mount("/assets", StaticFiles(directory=str(static_root)), name="assets")
    app.mount("/mcp", mcp_app, name="mcp")

    # RFC 9728 §3.1 puts the metadata for resource https://host/mcp at
    # https://host/.well-known/oauth-protected-resource/mcp. The bare path is served too
    # because clients that treat the appliance root as the resource look there instead;
    # both describe the one MCP endpoint this appliance exposes.
    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    def oauth_protected_resource() -> JSONResponse:
        """Unauthenticated by definition: a client reads this before it has a token."""
        return JSONResponse(
            protected_resource_metadata(config_store.get()),
            # Public metadata, and a browser-hosted MCP client fetches it cross-origin.
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def oauth_authorization_server_alias() -> RedirectResponse:
        """Compatibility for clients predating RFC 9728, which look for authorization
        server metadata on the resource server itself. This appliance is not an
        authorization server, so it points at the one that is."""
        issuer = config_store.get().security.oidc_issuer.rstrip("/")
        return RedirectResponse(f"{issuer}/.well-known/openid-configuration", status_code=307)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(static_root.joinpath("index.html")))

    @app.get("/healthz")
    def health() -> dict:
        return {"status": "ok", "service": "knowledge-index", "version": "0.2.0"}

    @app.get("/api/me")
    def me(request: Request) -> dict:
        identity = resolve_identity(request)
        return {
            "subject": identity.subject,
            "username": identity.username,
            "groups": identity.groups,
            "principals": sorted(identity.principals),
            "is_admin": identity.is_admin,
            "auth_mode": config_store.get().security.auth_mode,
            # Sidebar and connector panels link into the hosted documentation; served
            # here because /api/me is the one config-bearing endpoint non-admins can read.
            "docs_url": config_store.get().components.docs_url,
        }

    @app.get("/api/downloads/{token}/{filename}", include_in_schema=False)
    def download_original_document(token: str, filename: str, request: Request) -> FileResponse:
        """Stream an exact original blob through a short-lived MCP-issued capability.

        The capability itself is the credential.  Before every read we re-check the ACL
        snapshot using the principals captured when the MCP tool issued the link, so a
        revoked grant invalidates an otherwise unexpired URL.
        """

        capability = download_tokens.resolve(token)
        if capability is None or filename != capability.filename:
            raise HTTPException(status_code=404, detail="download link is invalid or expired")
        request.state.audit_principals = list(capability.principals)
        with session_factory() as session:
            citation = RetrievalService(session, config_store.get()).citation_for_version(
                capability.version_id,
                set(capability.principals),
                source_object_ids={capability.source_object_id},
            )
            if citation is None:
                download_tokens.revoke(token)
                raise HTTPException(status_code=404, detail="document access is no longer valid")
        blob_path = LocalArtifactStore(config_store.get().artifact_dir).path_for_hash(
            capability.content_hash
        )
        if not blob_path.is_file() or blob_path.stat().st_size != capability.size_bytes:
            download_tokens.revoke(token)
            raise HTTPException(status_code=410, detail="original document blob is unavailable")
        return FileResponse(
            blob_path,
            media_type=capability.mime_type,
            filename=capability.filename,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/api/status")
    def status(request: Request) -> dict:
        identity = resolve_identity(request)
        # This response is where an operator reads what is in flight, so it is where a
        # run nothing will ever advance has to stop being reported as in flight.
        _sweep_runs_if_due(session_factory, config_store.get())
        with session_factory() as session:
            access = AccessService(session)
            visible_documents = access.visible_document_ids(set(identity.principals))
            visible_projects = access.visible_project_ids(set(identity.principals))
            # Exactly the rule GET /api/sources applies, so the tiles and the list can
            # never disagree: an admin sees every connection, anyone else sees the ones
            # that are unowned or sit in a project they can reach.
            source_statement = select(Source.id)
            if not identity.is_admin:
                source_statement = source_statement.where(
                    (Source.project_id.is_(None)) | (Source.project_id.in_(visible_projects))
                )
            visible_source_ids = list(session.scalars(source_statement).all())
            # Two things are stored as "skipped" that are not the handler's judgement: a
            # stage parked behind its predecessor (so the claim query cannot hand it to a
            # worker) and a stage switched off in config. Reporting the first as "skipped"
            # told an operator the pipeline looked at every document and declined it;
            # reporting the second that way made the enabled toggle look inert, because
            # handler skips were counted as its work. Each gets its own bucket. See
            # taxonomies.stage_bucket, which does exactly this in Python for `ki status`.
            #
            # Bucketed in a subquery rather than inline: repeating the CASE in GROUP BY
            # would repeat its bind parameters, and Postgres treats two parameter sets as
            # two different expressions however identical the SQL around them looks.
            skip_reason = ProcessingState.last_error["reason"].as_string()
            buckets = select(
                ProcessingState.stage.label("stage"),
                case(
                    (
                        and_(
                            ProcessingState.status == ProcessingStatus.SKIPPED.value,
                            skip_reason == WAITING_FOR_PREVIOUS_STAGE,
                        ),
                        STAGE_BUCKET_WAITING,
                    ),
                    (
                        and_(
                            ProcessingState.status == ProcessingStatus.SKIPPED.value,
                            skip_reason == DISABLED_BY_CONFIGURATION,
                        ),
                        STAGE_BUCKET_DISABLED,
                    ),
                    else_=ProcessingState.status,
                ).label("bucket"),
            ).join(
                SourceObject,
                SourceObject.id == ProcessingState.source_object_id,
            ).where(
                SourceObject.deleted_at.is_(None)
            ).subquery()
            pipeline_rows = session.execute(
                select(buckets.c.stage, buckets.c.bucket, func.count())
                .group_by(buckets.c.stage, buckets.c.bucket)
                .order_by(buckets.c.stage, buckets.c.bucket)
            ).all()
            pipeline: dict[str, dict[str, int]] = {}
            for stage, state, count in pipeline_rows:
                pipeline.setdefault(stage, {})[state] = count
            active_runs = session.scalars(
                select(PipelineRunRecord)
                .where(PipelineRunRecord.status.in_(["queued", "running"]))
                .order_by(PipelineRunRecord.created_at.desc())
                .limit(8)
            ).all()
            return {
                "counts": {
                    # Scoped like GET /api/sources, which shows a non-admin only the
                    # sources that are unowned or in a project they can see. Counting every
                    # row here made the tile disagree with the list underneath it — the
                    # same mistake `matters` made, in the other direction.
                    "sources": len(visible_source_ids),
                    "source_objects": session.scalar(
                        select(func.count())
                        .select_from(SourceObject)
                        .where(
                            SourceObject.source_id.in_(visible_source_ids),
                            SourceObject.deleted_at.is_(None),
                        )
                    )
                    or 0,
                    "projects": len(visible_projects),
                    # Counted through the documents the caller can actually reach, not
                    # through project membership. Access here normally comes from mirrored
                    # source ACLs and a firm may run with no projects at all — as this one
                    # does — so the project filter reported 0 matters while 14 existed and
                    # were readable, and `list_matters` happily returned them.
                    "matters": session.scalar(
                        select(func.count(func.distinct(Document.matter_id))).where(
                            Document.id.in_(visible_documents),
                            Document.matter_id.is_not(None),
                        )
                    )
                    or 0,
                    "documents": len(visible_documents),
                    "chunks": session.scalar(
                        select(func.count())
                        .select_from(Chunk)
                        .where(Chunk.document_id.in_(visible_documents))
                    )
                    or 0,
                    "decisions": session.scalar(
                        select(func.count())
                        .select_from(DecisionRecord)
                        .where(DecisionRecord.document_id.in_(visible_documents))
                    )
                    or 0,
                    # A quarantined document the caller cannot read is not their problem
                    # and must not be counted at them; join through the source objects they
                    # can actually reach.
                    "quarantined": session.scalar(
                        select(func.count())
                        .select_from(ProcessingState)
                        .join(SourceObject, SourceObject.id == ProcessingState.source_object_id)
                        .where(
                            ProcessingState.status == "quarantined",
                            SourceObject.source_id.in_(visible_source_ids),
                            SourceObject.deleted_at.is_(None),
                        )
                    )
                    or 0,
                },
                "pipeline": pipeline,
                "runs": [_run_payload(row) for row in active_runs],
            }

    @app.get("/api/config")
    def get_config(request: Request) -> dict:
        resolve_identity(request, admin=True)
        return config_store.get().model_dump(mode="json")

    @app.get("/api/config/precedence")
    def config_precedence(request: Request) -> dict:
        """Which settings come from the environment rather than from the saved file.

        The effective config alone cannot answer "why did my edit not stick"; this can."""
        resolve_identity(request, admin=True)
        return config_store.precedence()

    @app.put("/api/config")
    def update_config(config: AppConfig, request: Request) -> dict:
        resolve_identity(request, admin=True)
        try:
            config_store.save(config)
        except EnvironmentPinnedSetting as exc:
            # Refused rather than written-and-ignored: an admin edit that silently loses
            # to a variable is precisely the failure this endpoint used to produce.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        saved = config_store.get()
        return {
            "saved": True,
            "config": saved.model_dump(mode="json"),
            "environment": config_store.precedence()["environment"],
        }

    @app.get("/api/ontology")
    def ontology_info(request: Request) -> dict:
        """The active ontology: artifact identity plus one section per active facet."""
        resolve_identity(request)
        config = config_store.get()
        browse = config.browse_ontology()
        artifacts = discover_artifacts(config.ontology_uploads_dir())
        facets = {}
        for facet in config.ontology.active_facets:
            facet_scope = config.ontology_facet(facet)
            facets[facet] = {
                "fingerprint": facet_scope.fingerprint,
                "visible_nodes": len(facet_scope.visible),
                "roots": facet_scope.roots(),
            }
        return {
            "artifact": {
                "name": browse.artifact.name,
                "version": browse.artifact.version,
                "source_sha256": browse.artifact.source_sha256,
                "total_nodes": len(browse.artifact.nodes),
            },
            "available_artifacts": sorted(artifacts),
            "active_facets": config.ontology.active_facets,
            "disabled_nodes": config.ontology.disabled_nodes,
            "fingerprint": browse.fingerprint,
            "visible_nodes": len(browse.visible),
            "facets": facets,
        }

    @app.get("/api/ontology/children")
    def ontology_children_api(node_id: str, request: Request) -> dict:
        """Children for the tree editor: full artifact view with scope flags.

        Unlike the extraction agent's tools (active scope only), the editor must
        show disabled nodes so they can be re-enabled: ``disabled`` marks an
        explicit toggle, ``hidden`` marks inherited invisibility."""
        resolve_identity(request)
        config = config_store.get()
        active = config.browse_ontology()
        full = ontology_scope(
            discover_artifacts(config.ontology_uploads_dir())[config.ontology.artifact],
            config.ontology.active_facets,
            (),
        )
        disabled = set(config.ontology.disabled_nodes)
        children = []
        for child in full.children(node_id):
            child["disabled"] = child["id"] in disabled
            child["hidden"] = child["id"] not in active.visible
            children.append(child)
        return {"node_id": node_id, "label": full.label_of(node_id), "children": children}

    @app.get("/api/ontology/search")
    def ontology_search_api(q: str, request: Request) -> list[dict]:
        resolve_identity(request)
        return config_store.get().browse_ontology().search(q, limit=20)

    @app.put("/api/ontology/scope")
    def update_ontology_scope(payload: OntologyScopeUpdate, request: Request) -> dict:
        """Change the artifact, facets, or node toggles — then selectively
        re-type only the documents whose node fell out of the visible scope."""
        resolve_identity(request, admin=True)
        config = config_store.get().model_copy(deep=True)
        if payload.artifact is not None:
            config.ontology.artifact = payload.artifact
        if payload.active_facets is not None:
            config.ontology.active_facets = payload.active_facets
        if payload.disabled_nodes is not None:
            config.ontology.disabled_nodes = sorted(set(payload.disabled_nodes))
        try:
            scope = config.browse_ontology()  # validates the artifact before saving
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config_store.save(config)
        requeued = PipelineRunner(session_factory, config).requeue_ontology_outdated()
        response: dict = {
            "saved": True,
            "fingerprint": scope.fingerprint,
            "visible_nodes": len(scope.visible),
            "requeued_documents": requeued,
        }
        if requeued:
            # Best-effort relaunch: an unreachable orchestrator must not fail the
            # scope change — requeued rows stay pending for the next trigger.
            try:
                response["run"] = _launch_insertion(session_factory, config_store)
            except HTTPException as exc:
                response["run"] = {"error": str(exc.detail)}
            except Exception as exc:
                response["run"] = {"error": str(exc)}
        return response

    @app.post("/api/ontology/artifacts")
    async def upload_ontology_artifact(request: Request, file: UploadFile = File(...)) -> dict:
        resolve_identity(request, admin=True)
        name = Path(file.filename or "").name
        if not name.endswith((".json", ".json.gz")):
            raise HTTPException(status_code=422, detail="artifact must be .json or .json.gz")
        uploads = config_store.get().ontology_uploads_dir()
        uploads.mkdir(parents=True, exist_ok=True)
        raw = await file.read()
        try:
            OntologyArtifact.parse(raw)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid ontology artifact: {exc}") from exc
        (uploads / name).write_bytes(raw)
        return {"saved": name, "available_artifacts": sorted(discover_artifacts(uploads))}

    @app.get("/api/health/doc-types")
    def doc_type_health(request: Request) -> dict:
        """Depth pressure per top-level branch, live during a run.

        Documents stuck at shallow nodes (depth <= 2) signal that the ontology
        lacks a fitting subtree there — the successor of the old catch-all
        metric, now pointing at the exact node needing extension."""
        resolve_identity(request)
        scope = config_store.get().doc_ontology()
        with session_factory() as session:
            type_rows = session.execute(
                select(Document.doc_type, func.count())
                .where(Document.doc_type.isnot(None))
                .group_by(Document.doc_type)
            ).all()
            untyped = session.scalar(
                select(func.count()).select_from(Document).where(Document.doc_type.is_(None))
            ) or 0
        root_ids = [root["id"] for root in scope.roots()]
        branches: dict[str, dict] = {
            scope.label_of(root) or root: {"total": 0, "shallow": 0} for root in root_ids
        }
        shallow_nodes: dict[str, dict] = {}
        stale = 0
        for node_id, count in type_rows:
            if node_id not in scope.visible:
                stale += count
                continue
            ancestors = scope.ancestors(node_id)
            depth = scope.depth_of(node_id)
            for root in root_ids:
                if root in ancestors:
                    label = scope.label_of(root) or root
                    branches[label]["total"] += count
                    if depth <= 2:
                        branches[label]["shallow"] += count
            if depth <= 2:
                entry = shallow_nodes.setdefault(
                    node_id,
                    {
                        "id": node_id,
                        "label": scope.label_of(node_id),
                        "depth": depth,
                        "count": 0,
                    },
                )
                entry["count"] += count
        for stats in branches.values():
            stats["share"] = (
                round(stats["shallow"] / stats["total"], 4) if stats["total"] else 0.0
            )
        alerts = [
            {
                "kind": "depth_pressure",
                "branch": label,
                "share": stats["share"],
                "total": stats["total"],
                "message": f"{stats['shallow']} of {stats['total']} {label} documents "
                f"({stats['share']:.0%}) sit at depth <= 2 — the ontology may lack a "
                "fitting subtree here",
            }
            for label, stats in branches.items()
            if stats["total"] >= 50 and stats["share"] > 0.25
        ]
        judged_total = sum(count for _node, count in type_rows) + untyped
        if judged_total >= 20 and untyped / judged_total > 0.1:
            alerts.append(
                {
                    "kind": "untyped_share",
                    "count": untyped,
                    "total": judged_total,
                    "message": f"{untyped} of {judged_total} documents found no home in "
                    "this ontology at all — the strongest signal to extend the artifact",
                }
            )
        return {
            "fingerprint": scope.fingerprint,
            "branches": branches,
            "shallow_nodes": sorted(shallow_nodes.values(), key=lambda n: -n["count"])[:20],
            "untyped_documents": untyped,
            "stale_typed_documents": stale,
            "alerts": alerts,
        }

    @app.get("/api/components")
    async def components(request: Request) -> list[dict]:
        resolve_identity(request, admin=True)
        values = config_store.get().components
        specs = [
            ("Model gateway", "LiteLLM", values.litellm_url, values.litellm_url),
            ("Document parsing", "Docling Serve", values.docling_url, None),
            ("Search index", "OpenSearch", values.opensearch_url, values.opensearch_url),
            (
                "Pipeline orchestrator",
                values.orchestrator_provider.title(),
                values.orchestrator_api_url,
                values.orchestrator_ui_url,
            ),
            ("Traces", "Langfuse", values.traces_api_url or values.traces_url, values.traces_url),
        ]
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
            statuses = await asyncio.gather(*(_probe(client, api_url) for _, _, api_url, _ in specs))
        return [
            {"role": role, "name": name, "api_url": api_url, "ui_url": ui_url, "status": status}
            for (role, name, api_url, ui_url), status in zip(specs, statuses)
        ]

    @app.get("/api/projects")
    def list_projects(request: Request) -> list[dict]:
        identity = resolve_identity(request)
        with session_factory() as session:
            access = AccessService(session)
            ids = access.visible_project_ids(set(identity.principals))
            projects = session.scalars(
                select(Project).where(Project.id.in_(ids)).order_by(Project.name)
            ).all()
            return [_project_payload(session, project, identity) for project in projects]

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate, request: Request) -> dict:
        identity = resolve_identity(request, admin=True)
        with session_factory() as session:
            if session.scalar(select(Project.id).where(Project.key == payload.key)):
                raise HTTPException(status_code=409, detail="project key already exists")
            project = Project(
                key=payload.key,
                name=payload.name,
                description=payload.description,
                status="active",
            )
            session.add(project)
            session.flush()
            session.add(
                ProjectGrant(
                    project_id=project.id,
                    principal=(payload.initial_principal or f"user:{identity.subject}").casefold(),
                    principal_kind="user",
                    effect="allow",
                    role="owner",
                    origin="manual",
                )
            )
            session.commit()
            return _project_payload(session, project, identity)

    @app.get("/api/projects/{project_id}/grants")
    def project_grants(project_id: str, request: Request) -> list[dict]:
        identity = resolve_identity(request)
        with session_factory() as session:
            if not AccessService(session).can_manage_project(project_id, set(identity.principals)):
                raise HTTPException(status_code=403, detail="project administrator required")
            rows = session.scalars(
                select(ProjectGrant)
                .where(ProjectGrant.project_id == project_id)
                .order_by(ProjectGrant.principal)
            ).all()
            return [_grant_payload(row) for row in rows]

    @app.post("/api/projects/{project_id}/grants", status_code=201)
    def add_project_grant(project_id: str, payload: GrantCreate, request: Request) -> dict:
        identity = resolve_identity(request)
        with session_factory() as session:
            if not AccessService(session).can_manage_project(project_id, set(identity.principals)):
                raise HTTPException(status_code=403, detail="project administrator required")
            if session.get(Project, project_id) is None:
                raise HTTPException(status_code=404, detail="project not found")
            row = ProjectGrant(
                project_id=project_id,
                principal=payload.principal.casefold(),
                principal_kind=payload.principal_kind,
                effect=payload.effect,
                role=payload.role,
                origin="manual",
            )
            session.add(row)
            session.flush()
            _refresh_project_chunk_acl(session, project_id)
            session.commit()
            return _grant_payload(row)

    @app.get("/api/documents")
    def documents(
        request: Request,
        project_id: str | None = None,
        query: str | None = None,
        doc_type: str | None = None,
        matter_id: str | None = None,
        version_status: str | None = None,
        language: str | None = None,
        limit: int = 500,
        offset: int = 0,
        detailed: bool = False,
    ) -> list[dict] | dict:
        """List the complete authorized document ledger.

        ``detailed`` opts the data viewer into a paginated envelope while preserving
        the original list response for API clients.  Counts and row metadata are
        assembled in batches so a 1,000-row page does not cause 1,000 version queries.
        """

        identity = resolve_identity(request)
        with session_factory() as session:
            principals = set(identity.principals)
            conditions = [AccessService(session).version_predicate(principals)]
            if project_id:
                conditions.append(Document.project_id == project_id)
            if query:
                conditions.append(Document.title.ilike(f"%{query.strip()}%"))
            if matter_id:
                conditions.append(Document.matter_id == matter_id)
            if version_status:
                conditions.append(DocumentVersion.status == version_status)
            if language:
                conditions.append(Document.language == language)

            row_conditions = list(conditions)
            if doc_type:
                row_conditions.append(Document.doc_type == doc_type)

            total = int(
                session.scalar(
                    select(func.count(func.distinct(Document.id)))
                    .select_from(Document)
                    .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                    .where(*row_conditions)
                )
                or 0
            )
            page_limit = min(max(limit, 1), 5_000)
            page_offset = max(offset, 0)
            statement = (
                select(Document)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .where(*row_conditions)
                .distinct()
            )
            rows = session.scalars(
                statement.order_by(Document.updated_at.desc(), Document.id)
                .offset(page_offset)
                .limit(page_limit)
            ).all()
            items = _document_payloads(session, rows, principals)
            if not detailed:
                return items

            doc_type_counts = session.execute(
                select(Document.doc_type, func.count(func.distinct(Document.id)))
                .select_from(Document)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .where(*conditions)
                .group_by(Document.doc_type)
                .order_by(func.count(func.distinct(Document.id)).desc(), Document.doc_type)
            ).all()
            language_counts = session.execute(
                select(Document.language, func.count(func.distinct(Document.id)))
                .select_from(Document)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .where(*conditions)
                .group_by(Document.language)
                .order_by(func.count(func.distinct(Document.id)).desc(), Document.language)
            ).all()
            return {
                "items": items,
                "pagination": {
                    "total": total,
                    "offset": page_offset,
                    "limit": page_limit,
                    "returned": len(items),
                    "has_more": page_offset + len(items) < total,
                },
                "facets": {
                    "doc_types": [
                        {"value": value, "count": count}
                        for value, count in doc_type_counts
                        if value
                    ],
                    "languages": [
                        {"value": value, "count": count}
                        for value, count in language_counts
                        if value
                    ],
                },
            }

    @app.get("/api/documents/{document_id}")
    def document(document_id: str, request: Request) -> dict:
        identity = resolve_identity(request)
        with session_factory() as session:
            principals = set(identity.principals)
            retrieval = RetrievalService(session, config_store.get())
            result = retrieval.get_document(
                document_id, principals=set(identity.principals)
            )
            if result is None:
                raise HTTPException(status_code=404, detail="document not found")
            document_row = session.get(Document, document_id)
            assert document_row is not None
            # Everything extract_metadata produced beyond the document row: the clauses it
            # identified, and the audit of what it set with what confidence. A firm
            # disputing a classification has nothing to go on without them.
            clauses = session.scalar(
                select(Artifact).where(
                    Artifact.content_hash == result["version"]["content_hash"],
                    Artifact.kind == "notable_clauses",
                )
            )
            result["clauses"] = (clauses.payload or {}).get("clauses") if clauses else None
            result["extractions"] = [
                {
                    "fields": row.fields,
                    "model": row.model,
                    "prompt_version": row.prompt_version,
                    "confidence": row.confidence,
                    "created_at": row.created_at.isoformat(),
                }
                for row in session.scalars(
                    select(Extraction)
                    .where(
                        Extraction.target_entity == "document",
                        Extraction.target_id == document_id,
                    )
                    .order_by(Extraction.created_at.desc())
                ).all()
            ]
            result["document"].update(
                {
                    # The ontology walk's own output. Stored since the ontology system
                    # landed and returned by nothing, so the two facts a lawyer would
                    # challenge a classification with — the path the classifier took and
                    # which ontology it took it under — were computed and then hidden.
                    "doc_type_ancestors": document_row.doc_type_ancestors,
                    "doc_type_label": _ontology_label(config_store.get(), document_row.doc_type),
                    "doc_type_path": _ontology_path(config_store.get(), document_row.doc_type),
                    "ontology_fingerprint": document_row.ontology_fingerprint,
                    "parties": document_row.parties,
                    "identifiers": document_row.identifiers,
                    "latest_final_version_id": document_row.latest_final_version_id,
                    "provenance": document_row.provenance,
                    "created_at": document_row.created_at.isoformat(),
                    "updated_at": document_row.updated_at.isoformat(),
                }
            )
            selected_version = session.get(DocumentVersion, result["version"]["id"])
            if selected_version is not None:
                result["version"].update(
                    {
                        "status_evidence": selected_version.status_evidence,
                        "redline_against": selected_version.redline_against,
                        "provenance": selected_version.provenance,
                        "created_at": selected_version.created_at.isoformat(),
                        "updated_at": selected_version.updated_at.isoformat(),
                    }
                )
            versions: list[dict] = []
            version_rows = session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .order_by(DocumentVersion.ordinal.desc().nullslast(), DocumentVersion.created_at)
            ).all()
            for version in version_rows:
                citation = retrieval.citation_for_version(version.id, principals)
                if citation is None:
                    continue
                versions.append(
                    {
                        "id": version.id,
                        "ordinal": version.ordinal,
                        "status": version.status,
                        "content_hash": version.content_hash,
                        "status_evidence": version.status_evidence,
                        "redline_against": version.redline_against,
                        "provenance": version.provenance,
                        "created_at": version.created_at.isoformat(),
                        "updated_at": version.updated_at.isoformat(),
                        "sources": citation["source_objects"],
                    }
                )
            result["versions"] = versions
            matter = session.get(Matter, document_row.matter_id) if document_row.matter_id else None
            result["matter"] = (
                {
                    "id": matter.id,
                    "project_id": matter.project_id,
                    "title": matter.title,
                    "reference_numbers": matter.reference_numbers,
                    "practice_area": matter.practice_area,
                    "matter_kind": matter.matter_kind,
                    "status": matter.status,
                    "responsible": matter.responsible,
                    "time_range": matter.time_range,
                    "imported": matter.imported,
                    "provenance": matter.provenance,
                }
                if matter
                else None
            )
            result["related"] = retrieval.find_related_documents(
                document_id,
                principals=principals,
                include_same_matter=True,
                limit=250,
            )
            result["grants"] = [
                _grant_payload(row)
                for row in session.scalars(
                    select(DocumentGrant).where(DocumentGrant.document_id == document_id)
                ).all()
            ]
            return result

    @app.post("/api/documents/{document_id}/grants", status_code=201)
    def add_document_grant(document_id: str, payload: GrantCreate, request: Request) -> dict:
        identity = resolve_identity(request)
        with session_factory() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise HTTPException(status_code=404, detail="document not found")
            # A document no project owns still needs an ethical wall — and connectors
            # produce plenty of them, because a project is assigned later or never.
            # Appliance administrators may grant on those directly; everyone else still
            # needs a managing role on the owning project, which an orphan has not got.
            access = AccessService(session)
            if not access.is_admin(set(identity.principals)) and not (
                document.project_id
                and access.can_manage_project(document.project_id, set(identity.principals))
            ):
                raise HTTPException(status_code=403, detail="project administrator required")
            row = DocumentGrant(
                document_id=document_id,
                principal=payload.principal.casefold(),
                principal_kind=payload.principal_kind,
                effect=payload.effect,
                role=payload.role,
                origin="manual",
            )
            session.add(row)
            session.flush()
            _refresh_document_chunk_acl(session, document_id)
            session.commit()
            return _grant_payload(row)

    @app.get("/api/graph")
    def graph(
        request: Request,
        project_id: str | None = None,
        query: str | None = None,
        doc_type: str | None = None,
        matter_id: str | None = None,
        version_status: str | None = None,
        language: str | None = None,
        limit: int = 0,
    ) -> dict:
        identity = resolve_identity(request)
        with session_factory() as session:
            return GraphService(session).projection(
                principals=set(identity.principals),
                project_id=project_id,
                query=query,
                doc_type=doc_type,
                matter_id=matter_id,
                version_status=version_status,
                language=language,
                limit=min(limit, 10_000) if limit > 0 else None,
            )

    @app.get("/api/matters")
    def matters(request: Request, query: str | None = None, limit: int = 20) -> list[dict]:
        """Matters by name, scoped to the documents the caller can actually read.

        The graph projection matches on ``Document.title`` only, so a matter surfaced in
        the command palette solely when one of its files happened to be named after it —
        typing a matter's own name found passages inside it but not the matter. This is
        the lookup that group needs.

        Scoped through the visible documents, never through project membership: access
        here normally comes from mirrored source ACLs and a firm may run with no projects
        at all, in which case a project filter reports every matter as unreadable.
        """
        identity = resolve_identity(request)
        with session_factory() as session:
            visible_documents = AccessService(session).visible_document_ids(
                set(identity.principals)
            )
            document_count = func.count(Document.id)
            statement = (
                select(Matter.id, Matter.title, Matter.practice_area, document_count)
                .join(Document, Document.matter_id == Matter.id)
                .where(Document.id.in_(visible_documents))
                .group_by(Matter.id, Matter.title, Matter.practice_area)
                .order_by(document_count.desc(), Matter.title)
                .limit(min(limit, 200))
            )
            if query and query.strip():
                statement = statement.where(Matter.title.ilike(f"%{query.strip()}%"))
            return [
                {
                    "id": matter_id,
                    "title": title,
                    "practice_area": practice_area,
                    # Documents of this matter the caller may read — not the matter's
                    # size. A matter whose readable count is zero never appears at all.
                    "documents": count,
                }
                for matter_id, title, practice_area, count in session.execute(statement).all()
            ]

    @app.post("/api/search")
    def search(payload: SearchRequest, request: Request) -> dict:
        identity = resolve_identity(request)
        with session_factory() as session:
            service = RetrievalService(session, config_store.get())
            filters = SearchFilters(
                project_id=payload.project_id,
                matter_id=payload.matter_id,
                doc_type=payload.doc_type,
                version_status=payload.version_status,
                language=payload.language,
            )
            if payload.query.strip():
                hits = service.search_semantic(
                    payload.query,
                    principals=set(identity.principals),
                    filters=filters,
                    limit=payload.limit,
                )
            else:
                hits = service.search_filter(
                    principals=set(identity.principals), filters=filters, limit=payload.limit
                )
            scope = AccessService(session).compile_scope(
                set(identity.principals),
                project_ids=[payload.project_id] if payload.project_id else [],
            )
            return {
                "scope": {
                    "fingerprint": scope.fingerprint,
                    "projects": len(scope.project_ids),
                    "documents": len(scope.document_ids),
                    "filters": {key: value for key, value in vars(filters).items() if value},
                },
                "hits": [hit.as_dict() for hit in hits],
            }

    @app.post("/api/ask")
    def ask(payload: AskRequest, request: Request) -> dict:
        """Reference agentic answer: plan retrieval, execute it under the caller's
        own principals, then synthesize a grounded, cited answer. Every retrieval
        leg is ACL-scoped to the caller — never admin. Model/gateway errors raise."""

        identity = resolve_identity(request)
        principals = set(identity.principals)
        config = config_store.get()

        # Planning and synthesis are answer-time spend; the retrieval legs in between
        # book themselves under "search".
        with usage_stage("ask"):
            plan = chat_json(
                config.ask_model,
                config,
                system=_ASK_PLANNER_SYSTEM,
                user=json_dumps(
                    {"question": payload.question, "project_id": payload.project_id}
                ),
                schema=Plan,
            )

        with session_factory() as session:
            service = RetrievalService(session, config)
            evidence = _run_ask_plan(service, plan, principals, payload.project_id)

        with usage_stage("ask"):
            answer = chat_json(
                config.ask_model,
                config,
                system=_ASK_SYNTHESIS_SYSTEM,
                user=json_dumps({"question": payload.question, "evidence": evidence}),
                schema=AskAnswer,
            )
        return {
            "answer": answer.answer,
            "citations": [citation.model_dump() for citation in answer.citations],
            "evidence": evidence,
            "plan": plan.model_dump(),
        }

    @app.get("/api/connectors/catalog")
    def connector_catalog(request: Request) -> list[dict]:
        resolve_identity(request, admin=True)
        from knowledge_index.connectors import catalog as connector_registry_catalog

        return _connector_catalog(config_store.get()) + connector_registry_catalog()

    @app.get("/api/connectors/{short_name}/fields")
    def connector_fields(short_name: str, request: Request) -> dict:
        """What the connect form must ask for: OAuth client credentials or a token."""
        resolve_identity(request, admin=True)
        from knowledge_index.connectors import get as get_connector
        from knowledge_index.connectors.runtime.errors import SourceError

        try:
            spec = get_connector(short_name)
        except SourceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "short_name": spec.short_name,
            "name": spec.label,
            "needs_oauth": bool(spec.oauth_provider),
            "mirrors_acls": spec.mirrors_acls,
            "incremental": spec.incremental,
            "notes": spec.notes,
            # Every deployment is bring-your-own-client: there is no shared OAuth app.
            "auth_fields": (
                [
                    {"name": "client_id", "title": "OAuth client id", "required": True},
                    {
                        "name": "client_secret",
                        "title": "OAuth client secret",
                        "required": True,
                        "secret": True,
                    },
                ]
                if spec.oauth_provider
                else [
                    {"name": "access_token", "title": "API token", "required": True, "secret": True}
                ]
            ),
            "config_fields": _config_fields(spec),
            "registration": _registration_guide(spec),
        }

    @app.post("/api/connectors/{source_id}/authorize")
    def start_authorization(source_id: str, request: Request) -> dict:
        """Re-authorize a connection that already exists.

        This is the revoked-or-expired-grant path, not the setup path: a brand-new OAuth
        connection has no source to re-authorize because ``POST /api/sources`` does not
        create one until the provider has answered. Starting a re-authorization changes
        nothing about the source — an operator who abandons it leaves a working
        connection working — so the status moves only when the callback succeeds.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.connectors import credentials as credential_store
        from knowledge_index.connectors import get as get_connector
        from knowledge_index.connectors import pending_auth
        from knowledge_index.connectors.runtime import oauth as oauth_runtime

        connectors_config = config_store.get().connectors
        with session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(status_code=404, detail="source not found")
            spec = get_connector(source.kind)
            if not spec.oauth_provider:
                raise HTTPException(status_code=422, detail=f"{source.kind} does not use OAuth")
            stored = credential_store.load(session, source_id)
            if not stored.get("client_id") or not stored.get("client_secret"):
                raise HTTPException(
                    status_code=422,
                    detail="this connection has no OAuth client credentials; re-create it "
                    "with the firm's own client id and secret",
                )
            provider = oauth_runtime.get_provider(spec.oauth_provider)
            authorization = oauth_runtime.build_authorization_request(
                provider,
                client_id=str(stored["client_id"]),
                redirect_uri=connectors_config.redirect_uri,
            )
            # The state and PKCE verifier must survive the redirect but are single-use
            # secrets, so they live with the credentials rather than in Source.config.
            # They carry their own deadline for the same reason a pending authorization
            # does: an abandoned handshake must not be redeemable a week later.
            expires_at = datetime.now(UTC) + pending_auth.TTL
            credential_store.save(
                session,
                source_id,
                {
                    **stored,
                    "oauth_state": authorization.state,
                    "oauth_code_verifier": authorization.code_verifier,
                    "oauth_state_expires_at": expires_at.isoformat(),
                },
                provider=source.kind,
            )
            session.commit()
            return {
                "authorization_url": authorization.url,
                "state": authorization.state,
                "expires_at": expires_at.isoformat(),
            }

    @app.get("/api/connectors/oauth/callback")
    async def oauth_callback(request: Request, code: str = "", state: str = ""):
        """Provider redirect target: exchange the code, then create or reactivate a source.

        Deliberately unauthenticated — the provider sends the browser here directly.
        Authorization rests on `state`, which is a 256-bit single-use value we minted and
        matched against exactly one handshake, in constant time.

        Two handshakes arrive here and they are matched in this order. A brand-new
        connection has no source yet: its ``state`` belongs to a pending authorization,
        and the Source and its credentials are created here, together, only once the
        provider has answered. A re-authorization belongs to a source that already
        exists; it is found by the state stored with its credentials and is never
        duplicated.
        """
        from knowledge_index.connectors import credentials as credential_store
        from knowledge_index.connectors import get as get_connector
        from knowledge_index.connectors import pending_auth
        from knowledge_index.connectors.runtime import oauth as oauth_runtime
        from knowledge_index.connectors.runtime.errors import SourceAuthError

        if not code or not state:
            raise HTTPException(status_code=422, detail="code and state are required")
        connectors_config = config_store.get().connectors
        with session_factory() as session:
            # Committed on its own: every path out of this handler below either commits
            # something else or raises, and housekeeping must not ride on that outcome.
            if pending_auth.sweep(session):
                session.commit()
            claimed = pending_auth.claim(session, state)
            if claimed is not None:
                record, intent = claimed
                spec = get_connector(record.kind)
                # The operator was away at the provider; the project they chose may have
                # been deleted meanwhile. Fail closed and take the handshake with it —
                # creating the source unfiled would silently widen who can reach it.
                if record.project_id and session.get(Project, record.project_id) is None:
                    pending_auth.discard(session, record)
                    session.commit()
                    raise HTTPException(
                        status_code=422,
                        detail="the project this connection was meant for no longer exists",
                    )
                try:
                    issued = await oauth_runtime.exchange_code(
                        oauth_runtime.get_provider(spec.oauth_provider or record.kind),
                        code=code,
                        client_id=str(intent["client_id"]),
                        client_secret=str(intent["client_secret"]),
                        redirect_uri=connectors_config.redirect_uri,
                        code_verifier=intent.get("oauth_code_verifier"),
                    )
                except SourceAuthError as exc:
                    # An authorization code is single-use, so a failed exchange ends this
                    # handshake for good. Leave nothing rather than a source that could
                    # never work: the operator connects again from the catalog.
                    pending_auth.discard(session, record)
                    session.commit()
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                source = Source(
                    project_id=record.project_id,
                    kind=record.kind,
                    display_name=record.display_name,
                    # Copied, not aliased: the row these came from is deleted below, and
                    # the new source must not share a mutable dict with a deleted object.
                    config=dict(record.config or {}),
                    sync_policy=dict(record.sync_policy or {}),
                    status="active",
                    provider=record.provider,
                    provider_connection_id=record.provider_connection_id,
                )
                session.add(source)
                session.flush()
                credential_store.save(
                    session,
                    source.id,
                    {
                        "client_id": intent["client_id"],
                        "client_secret": intent["client_secret"],
                        **issued,
                    },
                    provider=record.kind,
                )
                # One transaction: the source, its credentials, and the disappearance of
                # the handshake that produced them either all happen or none do.
                pending_auth.discard(session, record)
                session.commit()
                return RedirectResponse(
                    url=f"/?connected={source.kind}#connectors", status_code=303
                )

            matched: Source | None = None
            stored: dict = {}
            for candidate in session.scalars(
                select(Source).join(SourceCredential, SourceCredential.source_id == Source.id)
            ):
                try:
                    candidate_credentials = credential_store.load(session, candidate.id)
                except CredentialCryptoError:
                    # Encrypted under a key this deployment no longer holds, so it cannot
                    # be the source that minted this state. Skip it rather than let one
                    # unreadable row block every callback.
                    continue
                candidate_state = str(candidate_credentials.get("oauth_state") or "")
                if candidate_state and secrets.compare_digest(candidate_state, state):
                    matched, stored = candidate, candidate_credentials
                    break
            if matched is None:
                raise HTTPException(status_code=404, detail="no connection is awaiting this state")
            if _handshake_expired(stored):
                # Same rule as a pending authorization, enforced on lookup: burn the
                # stale state so it cannot be presented again.
                credential_store.save(
                    session,
                    matched.id,
                    _without_handshake(stored),
                    provider=matched.kind,
                )
                session.commit()
                raise HTTPException(status_code=410, detail="this authorization has expired")
            spec = get_connector(matched.kind)
            provider = oauth_runtime.get_provider(spec.oauth_provider or matched.kind)
            try:
                issued = await oauth_runtime.exchange_code(
                    provider,
                    code=code,
                    client_id=str(stored["client_id"]),
                    client_secret=str(stored["client_secret"]),
                    redirect_uri=connectors_config.redirect_uri,
                    code_verifier=stored.get("oauth_code_verifier"),
                )
            except SourceAuthError as exc:
                # The code is spent either way, so the state goes with it; the source
                # itself is untouched apart from reporting that it still needs attention.
                credential_store.save(
                    session, matched.id, _without_handshake(stored), provider=matched.kind
                )
                matched.status = "error"
                session.commit()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            credential_store.save(
                session,
                matched.id,
                {
                    "client_id": stored["client_id"],
                    "client_secret": stored["client_secret"],
                    **issued,
                },
                provider=matched.kind,
            )
            matched.status = "active"
            session.commit()
            # The provider sent a browser here, so hand it back to the admin UI rather
            # than rendering JSON at the end of a human-facing flow.
            return RedirectResponse(url=f"/?connected={matched.kind}#connectors", status_code=303)

    @app.get("/api/fs/list")
    def fs_list(request: Request, path: str = "") -> dict:
        """List the sub-directories of a server-visible path, so a local folder can be
        picked by clicking instead of typing. Admin only; read-only; directories only."""
        resolve_identity(request, admin=True)
        base = Path(path).expanduser() if path else _default_browse_root()
        try:
            resolved = base.resolve()
        except OSError:
            raise HTTPException(status_code=400, detail="invalid path") from None
        if not resolved.is_dir():
            raise HTTPException(status_code=404, detail="not a directory")
        dirs: list[str] = []
        try:
            for child in sorted(resolved.iterdir(), key=lambda item: item.name.lower()):
                if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
                    dirs.append(child.name)
        except PermissionError:
            pass
        parent = str(resolved.parent) if resolved.parent != resolved else None
        return {"path": str(resolved), "parent": parent, "dirs": dirs[:500]}

    @app.post("/api/fs/import-folder", status_code=201)
    async def import_browser_folder(
        request: Request,
        files: list[UploadFile] = File(...),
        relative_paths: str = Form(...),
    ) -> dict:
        """Copy a browser-selected directory into shared managed storage.

        Browsers deliberately do not reveal a selected directory's absolute host path.
        The native directory chooser therefore sends the selected files and their
        ``webkitRelativePath`` values here. App, worker, and watcher all mount the same
        ``artifact_dir`` parent, so the returned root is immediately usable by the
        ordinary local-filesystem connector without broadening container permissions.
        """
        resolve_identity(request, admin=True)
        try:
            paths = json.loads(relative_paths)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=422, detail="relative_paths must be JSON") from None
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            raise HTTPException(status_code=422, detail="relative_paths must be a string list")
        if len(files) != len(paths):
            raise HTTPException(status_code=422, detail="every uploaded file needs a relative path")
        if not 1 <= len(files) <= 10_000:
            raise HTTPException(status_code=422, detail="select a folder containing 1 to 10,000 files")

        safe_paths: list[Path] = []
        folder_name: str | None = None
        seen: set[str] = set()
        for raw_path in paths:
            pure = PurePosixPath(raw_path)
            parts = pure.parts
            if (
                pure.is_absolute()
                or len(parts) < 2
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in raw_path
            ):
                raise HTTPException(status_code=422, detail=f"unsafe relative path: {raw_path}")
            if folder_name is None:
                folder_name = parts[0]
            elif parts[0] != folder_name:
                raise HTTPException(status_code=422, detail="all files must come from one folder")
            relative = Path(*parts[1:])
            key = relative.as_posix()
            if key in seen:
                raise HTTPException(status_code=422, detail=f"duplicate relative path: {key}")
            seen.add(key)
            safe_paths.append(relative)

        config = config_store.get()
        import_parent = config.artifact_dir.expanduser().resolve().parent / "browser-sources"
        # An import is only half of adding a folder: the browser copies the bytes here,
        # then creates the source that points at them. Abandoning the second half used to
        # leave the firm's documents on disk with nothing referencing them and nothing
        # ever looking at them again. Reclaim those before writing more.
        with session_factory() as session:
            _sweep_unclaimed_imports(session, import_parent)
        destination = import_parent / uuid4().hex
        max_total_bytes = 512 * 1024 * 1024
        total_bytes = 0
        try:
            destination.mkdir(parents=True, exist_ok=False)
            for upload, relative in zip(files, safe_paths, strict=True):
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as handle:
                    while chunk := await upload.read(1024 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > max_total_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail="selected folder exceeds the 512 MiB import limit",
                            )
                        handle.write(chunk)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        finally:
            for upload in files:
                await upload.close()
        return {
            "root": str(destination),
            "folder_name": folder_name,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "managed": True,
        }

    @app.get("/api/sources/{source_id}/browse")
    def browse_source(source_id: str, request: Request, node: str = "") -> dict:
        """List a node's children so an operator can pick which folders to sync.

        Browsing uses the connection's own credentials, so it shows exactly what the
        firm's OAuth grant can reach — no more, and no less.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.connectors import get as get_connector
        from knowledge_index.connectors.runtime.errors import SourceError

        with session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(status_code=404, detail="source not found")
            try:
                spec = get_connector(source.kind)
            except SourceError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if not spec.supports_scoping:
                raise HTTPException(
                    status_code=422,
                    detail=f"{spec.label} syncs as a whole and cannot be scoped to folders",
                )
            if source.status == "pending_auth":
                raise HTTPException(
                    status_code=409,
                    detail=f"{spec.label} is still waiting for authorization; finish the "
                    "browser handshake before choosing folders",
                )
            # Building the connector reads and validates the stored credentials, so it
            # fails on a connection whose grant was revoked or never completed. That is
            # an operator-facing condition, not a bug: report it like any other browse
            # failure instead of letting it surface as a 500.
            try:
                connector = connector_from_source(source, session)
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator as-is
                raise HTTPException(status_code=502, detail=f"{spec.label}: {exc}") from exc
            try:
                children = connector.browse_children(node or None)
            except NotImplementedError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator as-is
                raise HTTPException(status_code=502, detail=f"{spec.label}: {exc}") from exc
            finally:
                connector.close()
            return {"parent": node or None, "nodes": children}

    @app.put("/api/sources/{source_id}/scope")
    def set_source_scope(source_id: str, payload: dict, request: Request) -> dict:
        """Replace the folders a source syncs.

        Narrowing the scope removes documents from the index on the next sync. That is the
        instruction, not an accident, so the change is recorded and the following sync
        tombstones what fell outside immediately — a drop that large would otherwise wait
        for later syncs to confirm it, and there is nothing to confirm about an
        instruction somebody just gave.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.connectors import scoping

        roots = payload.get("roots")
        if roots is not None and not isinstance(roots, list):
            raise HTTPException(status_code=422, detail="roots must be a list")
        with session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(status_code=404, detail="source not found")
            config = dict(source.config or {})
            connector_config = dict(config.get("connector") or {})
            before = scoping.describe(connector_config)
            connector_config[scoping.CONFIG_KEY] = scoping.parse_roots({"roots": roots or []})
            # Empty roots is an explicit whole-source choice after this endpoint is
            # called, not an unconfigured source waiting for the post-OAuth picker.
            connector_config[scoping.DECIDED_KEY] = True
            config["connector"] = connector_config
            source.config = config
            existing_objects = (
                session.scalar(
                    select(func.count())
                    .select_from(SourceObject)
                    .where(
                        SourceObject.source_id == source.id,
                        SourceObject.deleted_at.is_(None),
                    )
                )
                or 0
            )
            session.commit()
            after = scoping.describe(connector_config)
            changed = before["fingerprint"] != after["fingerprint"]
            would_remove_existing = changed and existing_objects > 0
            return {
                "source_id": source.id,
                "scope": after,
                "changed": changed,
                "existing_object_count": existing_objects,
                "would_remove_existing": would_remove_existing,
                "note": (
                    "Documents outside the new scope are removed on the next sync."
                    if would_remove_existing
                    else "Scope saved; there were no indexed documents to remove."
                    if changed
                    else "Scope unchanged."
                ),
            }

    @app.get("/api/sources")
    def list_sources(request: Request) -> list[dict]:
        identity = resolve_identity(request)
        with session_factory() as session:
            visible_projects = AccessService(session).visible_project_ids(set(identity.principals))
            statement = select(Source).order_by(Source.display_name)
            if not identity.is_admin:
                statement = statement.where(
                    (Source.project_id.is_(None)) | (Source.project_id.in_(visible_projects))
                )
            sources = session.scalars(statement).all()
            config = config_store.get()
            return [_source_payload(session, source, config) for source in sources]

    @app.post("/api/sources", status_code=201)
    def add_source(payload: SourceCreate, request: Request, response: Response) -> dict:
        """Create a connection — or, for an OAuth connector, only the intent to create one.

        An OAuth connector is not connected until the provider says so, so nothing is
        written to ``sources`` here: the answer is an authorization URL, and the Source
        is created by the callback that comes back from it. An operator who never
        finishes leaves nothing behind. Everything else — a local folder, a plugin drop,
        a connector authenticated with a token the operator just pasted — is complete the
        moment it is submitted and is created immediately, exactly as before.
        """
        identity = resolve_identity(request, admin=True)
        del identity
        config: dict[str, Any] = {}
        oauth_provider: str | None = None
        if payload.kind == "local_fs":
            if payload.root is None:
                raise HTTPException(status_code=422, detail="local source root is required")
            root = payload.root.expanduser().resolve()
            if not root.is_dir():
                raise HTTPException(
                    status_code=422, detail="source root must be an existing directory"
                )
            config["root"] = str(root)
        if payload.kind == "plugin_drop":
            # FDE-authored drop directory (docs/src/content/docs/development/plugin-connectors.md)
            if payload.root is None:
                raise HTTPException(status_code=422, detail="plugin drop root is required")
            root = payload.root.expanduser().resolve()
            if not root.is_dir():
                raise HTTPException(
                    status_code=422, detail="plugin drop root must be an existing directory"
                )
            config["root"] = str(root)
        source_status = "active"
        secret_values = {
            key: value
            for key, value in (
                ("client_id", payload.client_id),
                ("client_secret", payload.client_secret),
                ("access_token", payload.access_token),
            )
            if value
        }
        if payload.kind not in {"local_fs", "plugin_drop"}:
            from knowledge_index.connectors import get as get_connector
            from knowledge_index.connectors.runtime.errors import SourceError

            try:
                spec = get_connector(payload.kind)
            except SourceError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if spec.oauth_provider:
                if not secret_values.get("client_id") or not secret_values.get("client_secret"):
                    raise HTTPException(
                        status_code=422,
                        detail=f"{spec.label} needs the firm's own OAuth client id and secret",
                    )
                # Defers the Source to the callback: see this endpoint's docstring.
                oauth_provider = spec.oauth_provider
            elif not secret_values.get("access_token"):
                raise HTTPException(status_code=422, detail=f"{spec.label} needs an API token")
            _reject_unconfirmed_broad_grant(spec, payload)
            if payload.config:
                config["connector"] = {
                    key: value
                    for key, value in payload.config.items()
                    if value not in (None, "")
                }
        elif payload.config:
            config.update(
                {key: value for key, value in payload.config.items() if value not in (None, "")}
            )
        if payload.default_acl is not None:
            config["default_acl"] = payload.default_acl
        if payload.acl_by_path is not None:
            config["acl_by_path"] = payload.acl_by_path
        connection_id = config.get("source_connection_id") or payload.provider_connection_id
        with session_factory() as session:
            if payload.project_id and session.get(Project, payload.project_id) is None:
                raise HTTPException(status_code=422, detail="project does not exist")
            if oauth_provider:
                from knowledge_index.connectors import pending_auth
                from knowledge_index.connectors.runtime import oauth as oauth_runtime

                authorization = oauth_runtime.build_authorization_request(
                    oauth_runtime.get_provider(oauth_provider),
                    client_id=str(secret_values["client_id"]),
                    redirect_uri=config_store.get().connectors.redirect_uri,
                )
                pending = pending_auth.create(
                    session,
                    state=authorization.state,
                    kind=payload.kind,
                    display_name=payload.display_name,
                    project_id=payload.project_id,
                    config=config,
                    sync_policy=payload.sync_policy,
                    provider=payload.provider,
                    provider_connection_id=connection_id,
                    # The verifier is a single-use secret and is stored like one.
                    secret_values={
                        **secret_values,
                        "oauth_code_verifier": authorization.code_verifier,
                    },
                )
                expires_at = pending.expires_at.isoformat()
                session.commit()
                # 202, not 201: what was accepted is a handshake, not a connection.
                response.status_code = 202
                return {
                    "pending_authorization": True,
                    "authorization_url": authorization.url,
                    "state": authorization.state,
                    "expires_at": expires_at,
                    "kind": payload.kind,
                    "display_name": payload.display_name,
                }
            source = Source(
                project_id=payload.project_id,
                kind=payload.kind,
                display_name=payload.display_name,
                config=config,
                sync_policy=payload.sync_policy,
                status=source_status,
                provider=payload.provider,
                provider_connection_id=connection_id,
            )
            session.add(source)
            session.flush()
            if secret_values:
                from knowledge_index.connectors import credentials as credential_store

                credential_store.save(
                    session, source.id, secret_values, provider=payload.kind
                )
            session.commit()
            return _source_payload(session, source, config_store.get())

    @app.delete("/api/sources/{source_id}", status_code=200)
    def remove_source(source_id: str, request: Request) -> dict:
        """Disconnect a source and reclaim the bytes that belonged to it.

        An administrator who deletes a connection is telling the firm that the client's
        documents are gone. Until the staged copies and the content-addressed blobs go
        with the rows, that statement is false and the originals stay readable on the
        volume — so the response reports what was reclaimed, including anything that
        could not be.
        """
        resolve_identity(request, admin=True)
        config = config_store.get()
        # Remove provider subscriptions while the source OAuth credential still exists.
        # Failure does not strand the local disconnect: Google/Graph subscriptions expire
        # on their own, and the response reports what could not be removed upstream.
        from knowledge_index.connectors.events.manager import delete_upstream_for_source

        event_cleanup_failures = delete_upstream_for_source(
            session_factory, config, source_id
        )
        with session_factory() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(status_code=404, detail="source not found")
            managed_root = _managed_import_root(session, source, config)
            # Collected before the rows go: after the delete there is nothing left to
            # say which blobs this source's documents were.
            candidate_hashes = {
                value
                for value in session.scalars(
                    select(SourceObject.content_hash).where(
                        SourceObject.source_id == source_id,
                        SourceObject.content_hash.is_not(None),
                    )
                )
                if value
            }
            object_ids = select(SourceObject.id).where(SourceObject.source_id == source_id)
            removed = session.scalar(
                select(func.count()).select_from(SourceObject).where(
                    SourceObject.source_id == source_id
                )
            ) or 0
            # Remove the source's sync artifacts. Documents/chunks derived from it are
            # left in place but become unreachable — retrieval re-verifies every hit
            # against a live source object, so orphaned chunks fail closed (they can be
            # reclaimed by a later reindex). This keeps deletion fast and fail-safe.
            for child in (
                DocumentVersionSource.__table__,
                MatterAssignment.__table__,
                ProcessingState.__table__,
                SourceObjectGrant.__table__,
            ):
                session.execute(
                    delete(child).where(child.c.source_object_id.in_(object_ids))
                )
            session.execute(delete(SourceObject).where(SourceObject.source_id == source_id))
            session.execute(
                delete(PipelineRunRecord).where(PipelineRunRecord.source_id == source_id)
            )
            # The connector layer hangs two more tables off a source: its encrypted
            # credentials and the group memberships it mirrored. Both reference the
            # source row, so leaving them behind makes a connection undeletable — and
            # leaving a refresh token for a connection the firm has just disconnected
            # would be wrong even if the delete succeeded.
            session.execute(
                delete(SourceCredential).where(SourceCredential.source_id == source_id)
            )
            session.execute(
                delete(SourceGroupMember).where(SourceGroupMember.source_id == source_id)
            )
            # A deletion this source was still confirming references it too, and a
            # connection an administrator cannot disconnect because documents looked
            # deleted would be the worst possible way to learn this table exists.
            from knowledge_index.sync import deletions

            deletions.clear(session, source_id)
            session.delete(source)
            session.commit()

        # Deliberately after the commit. Unlinking first and then failing to commit would
        # destroy content belonging to a source that still exists; this order can only
        # leave bytes behind, and leftover bytes are reported rather than hidden.
        reclaimed = _reclaim_source_storage(
            session_factory,
            config,
            source_id=source_id,
            candidate_hashes=candidate_hashes,
            managed_root=managed_root,
        )
        return {
            "deleted": source_id,
            "removed_objects": removed,
            "storage": reclaimed.payload(),
            "event_cleanup": {
                "ok": not event_cleanup_failures,
                "failures": event_cleanup_failures,
            },
        }

    @app.get("/api/principals")
    def list_principals(request: Request) -> list[dict]:
        """Every principal an operator can grant to, and where each one was seen.

        Grants are matched by exact principal string, so a mistyped or wrong-cased
        principal silently grants nothing. Enumerating the real principals lets the
        grant UI autocomplete and validate against what the connectors actually
        mirrored, instead of relying on hand-typed strings.

        Existing grants alone are not a sufficient list: the directory group an
        operator is about to type by hand is precisely the one nobody has granted
        yet, so it has no grant row. Mirrored group memberships, registered service
        identities, and the configured admin groups are enumerated too — otherwise
        the picker can only offer principals that already work."""
        resolve_identity(request, admin=True)
        aggregate: dict[str, dict[str, Any]] = {}

        def record(
            principal: str | None,
            kind: str | None,
            origin: str,
            *,
            grants: int = 0,
            label: str | None = None,
        ) -> None:
            cleaned = (principal or "").strip()
            if not cleaned:
                return
            entry = aggregate.setdefault(
                cleaned,
                {
                    "principal": cleaned,
                    "principal_kind": kind or _principal_kind_hint(cleaned),
                    "grants": 0,
                    "origins": set(),
                    "label": None,
                },
            )
            entry["grants"] += grants
            entry["origins"].add(origin)
            if kind:
                entry["principal_kind"] = kind
            if label and not entry["label"]:
                entry["label"] = label

        with session_factory() as session:
            for model, origin in (
                (SourceObjectGrant, "source"),
                (ProjectGrant, "project"),
                (DocumentGrant, "document"),
            ):
                rows = session.execute(
                    select(model.principal, model.principal_kind, func.count()).group_by(
                        model.principal, model.principal_kind
                    )
                ).all()
                for principal, kind, count in rows:
                    record(principal, kind, origin, grants=int(count))
            # Mirrored directory groups are grantable before anyone has granted them,
            # and are the safest thing to grant: the string came out of the source
            # system rather than off an operator's keyboard.
            for group_id, group_name in session.execute(
                select(
                    SourceGroupMember.group_id, func.max(SourceGroupMember.group_name)
                ).group_by(SourceGroupMember.group_id)
            ).all():
                record(f"group:{group_id}", "group", "directory", label=group_name)
            # Bounded: a federated directory can hold tens of thousands of people, and
            # the picker only needs enough to autocomplete the common case.
            for member_id, member_type in session.execute(
                select(SourceGroupMember.member_id, SourceGroupMember.member_type)
                .where(SourceGroupMember.member_type == "user")
                .distinct()
                .order_by(SourceGroupMember.member_id)
                .limit(2000)
            ).all():
                record(f"user:{member_id}", "user", "directory")
            for client in session.scalars(select(ExternalClient)).all():
                record(client.principal, "service", "client", label=client.name)

        # The identity resolver mints these regardless of any grant row, so they are
        # always valid grant targets even on a brand new deployment.
        security = config_store.get().security
        for group in security.admin_groups or []:
            record(
                f"group:{group.casefold().strip('/')}",
                "group",
                "config",
                label="Configured administrator group",
            )
        record("role:admin", "role", "config", label="Any administrator")
        record("role:authenticated", "role", "config", label="Any authenticated caller")

        result = [
            {
                "principal": item["principal"],
                "principal_kind": item["principal_kind"],
                "grants": item["grants"],
                "origins": sorted(item["origins"]),
                "label": item["label"],
                # a principal seen on mirrored source ACLs is the safest to grant to
                "from_source": "source" in item["origins"],
            }
            for item in aggregate.values()
        ]
        result.sort(key=lambda item: (not item["from_source"], -item["grants"], item["principal"]))
        return result

    @app.get("/api/access/explain")
    def explain_access(
        request: Request, principal: str, query: str | None = None, limit: int = 60
    ) -> dict:
        """Why one principal can — or cannot — reach each document.

        "Ursula cannot see the engagement letter" is otherwise pure guesswork: the
        compiler fails closed, so a missing group membership, a mistyped grant and a
        deliberate denial all look identical from the outside. This returns the
        evidence behind the verdict: the groups the caller is expanded into, the
        grants that named a principal they hold, and — for a document they cannot
        reach — the principals that would have let them in.

        Read-only. The verdict itself always comes from the same scope compiler the
        retrieval paths use; the grant rows are shown as evidence, never re-evaluated
        here, so this endpoint cannot drift from the real decision.
        """
        resolve_identity(request, admin=True)
        asked = (principal or "").strip()
        if not asked:
            raise HTTPException(status_code=400, detail="principal is required")
        with session_factory() as session:
            access = AccessService(session)
            held = access.resolve_principals({asked})
            values = sorted(held)
            predicate = access.version_predicate({asked})

            statement = select(Document).order_by(Document.updated_at.desc())
            if query and query.strip():
                statement = statement.where(Document.title.ilike(f"%{query.strip()}%"))
            documents = session.scalars(statement.limit(min(max(limit, 1), 200))).all()
            doc_ids = [item.id for item in documents]
            visible_here = set(
                session.scalars(
                    select(Document.id)
                    .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                    .where(Document.id.in_(doc_ids), predicate)
                    .distinct()
                ).all()
            )
            visible_total = (
                session.scalar(
                    select(func.count(func.distinct(Document.id)))
                    .select_from(Document)
                    .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                    .where(predicate)
                )
                or 0
            )
            total = session.scalar(select(func.count()).select_from(Document)) or 0

            # One pass over the mirrored ACL of every listed document: the same rows
            # answer "what let them in" and "what would have".
            acl: dict[str, dict[str, Any]] = {
                item: {"allow": {}, "deny": set(), "sources": set(), "paths": set()}
                for item in doc_ids
            }
            for document_id, granted, effect, source_name, path in session.execute(
                select(
                    Document.id,
                    SourceObjectGrant.principal,
                    SourceObjectGrant.effect,
                    Source.display_name,
                    SourceObject.path,
                )
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(DocumentVersionSource, DocumentVersionSource.version_id == DocumentVersion.id)
                .join(SourceObject, SourceObject.id == DocumentVersionSource.source_object_id)
                .join(Source, Source.id == SourceObject.source_id)
                .outerjoin(SourceObjectGrant, SourceObjectGrant.source_object_id == SourceObject.id)
                .where(Document.id.in_(doc_ids), SourceObject.deleted_at.is_(None))
            ).all():
                entry = acl[document_id]
                entry["sources"].add(source_name)
                if path:
                    entry["paths"].add(path)
                if granted and effect == "allow":
                    entry["allow"].setdefault(granted, 0)
                elif granted:
                    entry["deny"].add(granted)

            # Mirrored membership counts turn a bare GUID into something an operator can
            # act on: a group that grants a document but has no mirrored members admits
            # nobody, and that is invisible from the grant row alone.
            member_counts = dict(
                session.execute(
                    select(SourceGroupMember.group_id, func.count()).group_by(
                        SourceGroupMember.group_id
                    )
                ).all()
            )

            local: dict[str, list[dict]] = {"project": [], "document": []}
            project_grants = (
                session.execute(
                    select(ProjectGrant, Project.name, Project.key)
                    .join(Project, Project.id == ProjectGrant.project_id)
                    .where(ProjectGrant.principal.in_(values))
                ).all()
                if values
                else []
            )
            for row, name, key in project_grants:
                local["project"].append(
                    {
                        "scope": "project",
                        "target_id": row.project_id,
                        "target": f"{key} · {name}",
                        "principal": row.principal,
                        "effect": row.effect,
                        "role": row.role,
                        "origin": row.origin,
                    }
                )
            document_grants = (
                session.execute(
                    select(DocumentGrant, Document.title)
                    .join(Document, Document.id == DocumentGrant.document_id)
                    .where(DocumentGrant.principal.in_(values))
                ).all()
                if values
                else []
            )
            for row, title in document_grants:
                local["document"].append(
                    {
                        "scope": "document",
                        "target_id": row.document_id,
                        "target": title or row.document_id,
                        "principal": row.principal,
                        "effect": row.effect,
                        "role": row.role,
                        "origin": row.origin,
                    }
                )
            local_by_document: dict[str, list[dict]] = defaultdict(list)
            for item in local["document"]:
                local_by_document[item["target_id"]].append(item)
            local_by_project: dict[str, list[dict]] = defaultdict(list)
            for item in local["project"]:
                local_by_project[item["target_id"]].append(item)

            # Which mirrored groups the caller was expanded into, and on whose authority.
            lookup = asked.partition(":")[2].strip().casefold()
            group_ids = [item.partition(":")[2] for item in values if item.startswith("group:")]
            memberships = (
                session.scalars(
                    select(SourceGroupMember).where(SourceGroupMember.group_id.in_(group_ids))
                ).all()
                if group_ids
                else []
            )
            source_names = dict(session.execute(select(Source.id, Source.display_name)).all())
            granted_objects = dict(
                session.execute(
                    select(SourceObjectGrant.principal, func.count(func.distinct(Document.id)))
                    .join(SourceObject, SourceObject.id == SourceObjectGrant.source_object_id)
                    .join(
                        DocumentVersionSource,
                        DocumentVersionSource.source_object_id == SourceObject.id,
                    )
                    .join(DocumentVersion, DocumentVersion.id == DocumentVersionSource.version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(SourceObjectGrant.effect == "allow", SourceObject.deleted_at.is_(None))
                    .group_by(SourceObjectGrant.principal)
                ).all()
            )
            by_group: dict[str, dict[str, Any]] = {}
            for row in memberships:
                entry = by_group.setdefault(
                    row.group_id,
                    {
                        "principal": f"group:{row.group_id}",
                        "group_id": row.group_id,
                        # The connector stores the directory's own name for the group; for
                        # Entra that is currently the GUID again, so the member list below
                        # is what actually identifies it. Never invent a nicer one.
                        "label": (
                            row.group_name
                            if row.group_name
                            and row.group_name
                            not in {row.group_id, row.group_id.rpartition(":")[2]}
                            else None
                        ),
                        "source": source_names.get(row.source_id) or row.source_id,
                        "members": [],
                        "member_count": 0,
                        "direct": False,
                        "documents": granted_objects.get(f"group:{row.group_id}", 0),
                    },
                )
                entry["member_count"] += 1
                if len(entry["members"]) < 8:
                    entry["members"].append(row.member_id)
                if row.member_id == lookup:
                    entry["direct"] = True
            groups = sorted(by_group.values(), key=lambda item: -item["documents"])

            items = []
            for document in documents:
                entry = acl[document.id]
                allowed = [
                    {"scope": "source", "principal": name}
                    for name in sorted(entry["allow"])
                    if name in held
                ]
                denied = [
                    {"scope": "source", "principal": name}
                    for name in sorted(entry["deny"])
                    if name in held
                ]
                for grant in local_by_document.get(document.id, []) + local_by_project.get(
                    document.project_id or "", []
                ):
                    (allowed if grant["effect"] == "allow" else denied).append(grant)
                items.append(
                    {
                        "id": document.id,
                        "title": document.title or document.id,
                        "source": sorted(entry["sources"])[0] if entry["sources"] else None,
                        "path": sorted(entry["paths"])[0] if entry["paths"] else None,
                        "visible": document.id in visible_here,
                        "allowed_by": allowed,
                        "denied_by": denied,
                        # What the source says would open it — the fix for a blocked
                        # document is a membership at source, not a grant typed here.
                        "source_allows": [
                            {
                                "principal": name,
                                "members": member_counts.get(name.partition(":")[2], 0),
                            }
                            for name in sorted(entry["allow"])
                        ],
                    }
                )

            return {
                "principal": asked,
                "resolved": values,
                "is_admin": access.is_admin(held),
                "source_acl_mode": access.source_acl_mode,
                "groups": groups,
                "local_grants": local["project"] + local["document"],
                "documents": {
                    "visible": visible_total,
                    "total": total,
                    "listed": len(items),
                    "items": items,
                },
            }

    @app.post("/api/actions/sync", status_code=202)
    def sync_sources(request: Request, payload: SyncRequest | None = None) -> dict:
        """Reserve one orchestrated sync run per eligible source and return immediately.

        This never scans inline. A first sync of a real estate is minutes of downloads;
        holding the request open for it puts a firm's onboarding at the mercy of whatever
        proxy timeout sits in front of the appliance, and gives the operator nothing to
        look at while it runs. The response is the list of run ids to watch on
        ``/api/runs``, plus the sources that were not started and why.
        """
        resolve_identity(request, admin=True)
        # Forced, not throttled: a source with a stranded sync run is refused a second
        # one by uq_pipeline_runs_active_sync, so this is the request that has to see the
        # stranded run resolved before it asks.
        _sweep_runs_if_due(session_factory, config_store.get(), force=True)
        try:
            result = sync_runs.enqueue_sync(
                session_factory,
                config_store.get(),
                source_id=payload.source_id if payload else None,
            )
        except sync_runs.UnknownSource as exc:
            raise HTTPException(status_code=404, detail=f"source not found: {exc}") from exc
        return result.payload()

    @app.post("/api/actions/pipeline")
    def run_pipeline(request: Request) -> dict:
        resolve_identity(request, admin=True)
        return _launch_insertion(session_factory, config_store)

    @app.post("/api/actions/build-environments")
    def build_environments(request: Request, limit: int | None = None) -> dict:
        """Propose sparse, firm-work-product RL-environment candidates for partner review."""
        resolve_identity(request, admin=True)
        from knowledge_index.pipeline.environments import EnvironmentBuilder

        result = EnvironmentBuilder(session_factory, config_store.get()).build(limit=limit)
        return result.__dict__

    @app.post("/api/actions/extract-billing")
    def extract_billing(request: Request, limit: int | None = None) -> dict:
        """Extract LEDES/UTBMS billing rows from invoice documents; dedup + insert."""
        resolve_identity(request, admin=True)
        from knowledge_index.pipeline.billing import BillingExtractor

        return BillingExtractor(session_factory, config_store.get()).extract(limit=limit).__dict__

    @app.get("/api/billing/invoices")
    def billing_invoices(request: Request, limit: int = 100) -> list[dict]:
        resolve_identity(request, admin=True)
        with session_factory() as session:
            invoices = session.scalars(
                select(BillingInvoice).order_by(BillingInvoice.created_at.desc()).limit(limit)
            ).all()
            payload = []
            for invoice in invoices:
                matter = session.get(Matter, invoice.matter_id) if invoice.matter_id else None
                line_count = (
                    session.scalar(
                        select(func.count())
                        .select_from(BillingLineItem)
                        .where(BillingLineItem.invoice_id == invoice.id)
                    )
                    or 0
                )
                payload.append(
                    {
                        "id": invoice.id,
                        "invoice_number": invoice.invoice_number,
                        "invoice_date": invoice.invoice_date.isoformat()
                        if invoice.invoice_date
                        else None,
                        "matter": matter.title if matter else None,
                        "matter_id": invoice.matter_id,
                        "invoice_total": invoice.invoice_total,
                        "currency": invoice.currency,
                        "line_items": line_count,
                    }
                )
            return payload

    @app.get("/api/billing/rollup/{matter_id}")
    def billing_rollup_endpoint(matter_id: str, request: Request) -> dict:
        resolve_identity(request, admin=True)
        from knowledge_index.pipeline.billing import billing_rollup

        with session_factory() as session:
            return billing_rollup(session, matter_id)

    @app.get("/api/entities/resolve")
    def resolve_entity_endpoint(request: Request, q: str = "") -> list[dict]:
        resolve_identity(request, admin=True)
        from knowledge_index.pipeline.billing import resolve_entity

        with session_factory() as session:
            return resolve_entity(session, q)

    @app.get("/api/environments")
    def list_environments(request: Request, status: str | None = None) -> list[dict]:
        resolve_identity(request, admin=True)
        with session_factory() as session:
            statement = select(EvalRecord).order_by(EvalRecord.created_at.desc())
            if status:
                statement = statement.where(EvalRecord.status == status)
            return [_environment_payload(session, record) for record in session.scalars(statement)]

    @app.patch("/api/environments/{env_id}")
    def update_environment(env_id: str, payload: EnvironmentUpdate, request: Request) -> dict:
        """Edit a candidate's task, criteria, or verifiers so it becomes precise and verifiable."""
        resolve_identity(request, admin=True)
        with session_factory() as session:
            record = session.get(EvalRecord, env_id)
            if record is None:
                raise HTTPException(status_code=404, detail="environment not found")
            if payload.instruction is not None:
                record.instruction = payload.instruction
            if payload.task_type is not None:
                record.task_type = payload.task_type
            if payload.practice_area is not None:
                record.practice_area = payload.practice_area
            if payload.holdout is not None:
                record.holdout = payload.holdout
            if payload.review_note is not None:
                record.review_note = payload.review_note
            if payload.rubric is not None:
                record.rubric = _validate_rubric(payload.rubric)
            if payload.verifiers is not None:
                record.verifiers = _validate_verifiers(payload.verifiers)
            session.commit()
            return _environment_payload(session, record)

    @app.post("/api/environments/{env_id}/approve")
    def approve_environment(env_id: str, request: Request) -> dict:
        identity = resolve_identity(request, admin=True)
        return _decide_environment(session_factory, env_id, "approved", identity)

    @app.post("/api/environments/{env_id}/reject")
    def reject_environment(env_id: str, request: Request) -> dict:
        identity = resolve_identity(request, admin=True)
        return _decide_environment(session_factory, env_id, "rejected", identity)

    @app.get("/api/index/status")
    def index_status(request: Request) -> dict:
        resolve_identity(request, admin=True)
        config = config_store.get()
        with session_factory() as session:
            chunk_count = session.scalar(select(func.count()).select_from(Chunk)) or 0
        return {
            "index_name": config.retrieval.index_name,
            "derived_index_name": config.derived_index_name(),
            "embedding_model": config.retrieval.embedding_model,
            "embedding_dimensions": config.retrieval.embedding_dimensions,
            "embedding_signature": config.embedding_signature(),
            "vector_engine": config.retrieval.vector_engine,
            "chunk_count": chunk_count,
            # Once vectors exist, the embedding model is locked until a rebuild: mixing
            # models in one ANN index is not permitted.
            "locked": chunk_count > 0,
        }

    @app.get("/api/models/catalog")
    def models_catalog(request: Request) -> dict:
        """What the gateway actually serves, so a model is picked and not typed.

        Proxied rather than called from the browser: reading the registry needs the
        gateway master key, and that key never leaves this process."""
        resolve_identity(request, admin=True)
        config = config_store.get()
        base = gateway_url(config)
        try:
            headers = gateway_admin_headers()
            response = httpx.get(f"{base}/model/info", headers=headers, timeout=8)
            response.raise_for_status()
            entries = [_gateway_model_entry(item) for item in response.json().get("data", [])]
        except Exception as exc:  # gateway may be briefly unreachable; surface, don't crash the UI
            return {
                "models": [],
                "entries": [],
                "credentials": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        # Credentials only gate *adding* a model. Losing them must not empty the model
        # lists, which is the page's primary job.
        error = None
        try:
            credentials = _gateway_credentials(base, headers)
        except Exception as exc:
            credentials = []
            error = f"provider credentials unavailable: {type(exc).__name__}: {exc}"
        entries.sort(key=lambda entry: entry["id"])
        return {
            # Plain id list kept for callers that only need names.
            "models": [entry["id"] for entry in entries],
            "entries": entries,
            "credentials": credentials,
            "error": error,
        }

    @app.post("/api/models/catalog", status_code=201)
    def add_gateway_model(payload: ModelRegistration, request: Request) -> dict:
        """Register a model with the gateway so stages can be assigned it.

        Requires ``store_model_in_db`` on the gateway; when it is off LiteLLM says so
        and that message is passed through verbatim rather than reported as success."""
        identity = resolve_identity(request, admin=True)
        config = config_store.get()
        base = gateway_url(config)
        params: dict[str, Any] = {"model": payload.model}
        if payload.credential_name:
            # The gateway accepts an unknown credential name and only fails at call
            # time with a provider auth error. Reject it here so a misconfigured model
            # cannot reach the pipeline and quarantine documents for the wrong reason.
            try:
                configured = _gateway_credentials(base, gateway_admin_headers())
                known = {entry["name"] for entry in configured}
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"model gateway unreachable: {exc}"
                ) from exc
            if payload.credential_name not in known:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"unknown gateway credential {payload.credential_name!r}; "
                        f"configured: {', '.join(sorted(known)) or 'none'}"
                    ),
                )
            params["litellm_credential_name"] = payload.credential_name
        if payload.api_base:
            params["api_base"] = payload.api_base
        body = {
            "model_name": payload.model_name,
            "litellm_params": params,
            "model_info": {"mode": payload.mode},
        }
        try:
            response = httpx.post(
                f"{base}/model/new", headers=gateway_admin_headers(), json=body, timeout=20
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"model gateway unreachable: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise HTTPException(
                status_code=response.status_code, detail=_gateway_error(response)
            )
        with session_factory() as session:
            session.add(
                AuditEvent(
                    actor_principals=sorted(identity.principals),
                    action="models.register",
                    target_type="model",
                    target_id=payload.model_name,
                    outcome="success",
                    # The credential is a name, not a secret; recording it makes it
                    # auditable which provider account a model was wired to.
                    details={
                        "model": payload.model,
                        "credential_name": payload.credential_name,
                        "api_base": payload.api_base,
                        "mode": payload.mode,
                    },
                )
            )
            session.commit()
        return {"registered": payload.model_name}

    @app.post("/api/actions/reindex")
    def reindex(request: Request) -> dict:
        """Rebuild the vector index against the current embedding model+dimension.

        Switches the chunk index to a name bound to the embedding signature (so two
        models never share one index), requeues the ``index`` stage for every object,
        and launches the pipeline to re-embed from the stored structured text. Upstream
        stages (conversion, classification, extraction) are not replayed."""
        resolve_identity(request, admin=True)
        config = config_store.get().model_copy(deep=True)
        target_index = config.derived_index_name()
        with session_factory() as session:
            chunk_count = session.scalar(select(func.count()).select_from(Chunk)) or 0
        config.retrieval.index_name = target_index
        # Bumps the operator-owned token, not producer_version: that is recomputed from the
        # code's own version on every load, so writing it here would be discarded and the
        # rebuild would requeue nothing.
        index_stage = config.pipeline.stage("index")
        config.pipeline.stages["index"] = index_stage.model_copy(
            update={"rerun_token": _bump_version(index_stage.rerun_token or "0")}
        )
        config_store.save(config)
        requeued = PipelineRunner(session_factory, config).requeue_outdated_stages()
        launch = _launch_insertion(session_factory, config_store)
        return {
            "target_index": target_index,
            "embedding_model": config.retrieval.embedding_model,
            "embedding_dimensions": config.retrieval.embedding_dimensions,
            "chunks_to_reembed": chunk_count,
            "requeued_objects": requeued,
            "run": launch,
        }

    # ------------------------------------------------------------------------- backup

    @app.get("/api/backup/preflight")
    def backup_preflight(request: Request) -> dict:
        """Everything that has to be true for the next backup to work, checked now.

        The way this feature fails is not a crash — it is running nightly for eight months
        against a share that was unmounted in March. This is the endpoint that turns that
        into something an operator finds out on a Tuesday afternoon.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import runs as backup_runs

        return backup_runs.preflight(config_store.get(), session_factory)

    @app.get("/api/backup/backups")
    def backup_list(request: Request, limit: int = 50) -> list[dict]:
        """Every complete backup at the configured destination, newest first."""
        resolve_identity(request, admin=True)
        from knowledge_index.backup import runs as backup_runs
        from knowledge_index.backup.destinations import DestinationError

        try:
            return backup_runs.list_backups(config_store.get(), limit=min(limit, 200))
        except DestinationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/actions/backup", status_code=202)
    def run_backup(request: Request, payload: BackupRequest | None = None) -> dict:
        """Reserve one backup run and return immediately.

        Same contract as ``/api/actions/sync`` and for the same reason: capturing and
        transferring the whole appliance is hours of work, and the response is the run id
        to watch on ``/api/runs``.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import runs as backup_runs

        # A stranded backup run refuses every later one, so this is the request that has
        # to see it resolved before it asks.
        _sweep_runs_if_due(session_factory, config_store.get(), force=True)
        try:
            enqueued = backup_runs.enqueue_backup(
                session_factory,
                config_store.get(),
                trigger="api",
                force=bool(payload.force) if payload else False,
            )
        except backup_runs.BackupNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except backup_runs.BackupRunFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return enqueued.payload()

    @app.post("/api/actions/backup-verify")
    def verify_backup(request: Request, payload: BackupIdRequest) -> dict:
        """Read a backup back from the destination and re-check every checksum.

        Synchronous, unlike taking one: verification reads at network speed with no
        dumping or compression in front of it, and an operator asking "is this one good"
        is waiting for the answer. Large backups are verified from the CLI instead.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import runs as backup_runs

        try:
            return backup_runs.verify_backup(
                config_store.get(), payload.backup_id, session_factory=session_factory
            )
        except backup_runs.BackupNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/backup/restorable")
    def backup_restorable(request: Request, path: str | None = None) -> dict:
        """Backups readable from a folder, so a recovery can point at a drive it just mounted."""
        resolve_identity(request, admin=True)
        from knowledge_index.backup import restore_runs
        from knowledge_index.backup.destinations import DestinationError

        try:
            found = restore_runs.list_backups_at(config_store.get(), path, session_factory)
        except restore_runs.RestoreNotAllowed as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except DestinationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"path": path or "", "backups": found}

    @app.post("/api/actions/restore", status_code=202)
    def run_restore(request: Request, payload: RestoreRequest) -> dict:
        """Reserve one restore run and return immediately.

        Staging is safe and is the whole point of exposing this: a firm that has never
        tried a restore does not have backups, it has files. Applying is the same endpoint
        with explicit per-store flags, refused up front if the plan has any blocker.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import runs as backup_runs_module
        from knowledge_index.backup import restore_runs

        _sweep_runs_if_due(session_factory, config_store.get(), force=True)
        try:
            enqueued = restore_runs.enqueue_restore(
                session_factory,
                config_store.get(),
                backup_id=payload.backup_id,
                source_path=payload.source_path or None,
                apply_databases=payload.apply_databases,
                apply_files=payload.apply_files,
                apply_search_index=payload.apply_search_index,
                apply_volumes=payload.apply_volumes,
            )
        except restore_runs.RestoreNotAllowed as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except backup_runs_module.BackupNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return enqueued.payload()

    @app.post("/api/actions/backup-restore-plan")
    def backup_restore_plan(request: Request, payload: BackupIdRequest) -> dict:
        """What restoring this backup would do, and what stands in the way.

        Reporting only — nothing here writes. Applying a restore is deliberately not an
        admin-UI button: it destroys the running appliance, and half of it needs
        containers stopped, so it lives in ``ki backup-restore`` and
        ``scripts/restore-backup.sh`` where an operator has to mean it.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import restore as backup_restore
        from knowledge_index.backup import runs as backup_runs

        try:
            return backup_restore.restore_plan(
                config_store.get(),
                payload.backup_id,
                session_factory,
                source_path=payload.source_path or None,
            )
        except backup_runs.BackupNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/backup/folders")
    def backup_folders(request: Request, path: str | None = None) -> dict:
        """The folders this appliance can put backups in, for picking rather than typing.

        A path typed into a text box is a path nobody can check until the first backup
        fails on it, and it asks the wrong question anyway — where the folder is *inside
        this container* is not something the person configuring backups has a reason to
        know.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import browse

        configured = config_store.get().backup.destination.path
        try:
            body = browse.listing(path, configured) if path else {"path": None, "entries": []}
        except browse.BrowseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        body["places"] = browse.places(configured)
        return body

    @app.post("/api/backup/folders")
    def create_backup_folder(request: Request, payload: BackupFolderRequest) -> dict:
        """Make a sub-folder, so choosing one does not mean leaving the page."""
        resolve_identity(request, admin=True)
        from knowledge_index.backup import browse

        configured = config_store.get().backup.destination.path
        try:
            body = browse.create(payload.path, payload.name, configured)
        except browse.BrowseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        body["places"] = browse.places(configured)
        return body

    @app.get("/api/backup/secrets")
    def backup_secret_status(request: Request) -> dict:
        """Which backup secrets are set, where each comes from, and its fingerprint.

        Never a value. A secret that can be read back out of an API is a secret that ends
        up in a browser history, a proxy log and a screenshot.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import secrets as backup_secrets

        return {
            "secrets": [
                backup_secrets.status(name, session_factory).payload()
                for name in backup_secrets.KNOWN
            ]
        }

    @app.post("/api/backup/secrets")
    def set_backup_secret(request: Request, payload: BackupSecretRequest) -> dict:
        """Store or forget one backup secret."""
        resolve_identity(request, admin=True)
        from knowledge_index.backup import secrets as backup_secrets

        try:
            if payload.value is None or not payload.value.strip():
                status = backup_secrets.forget(payload.name, session_factory)
            else:
                status = backup_secrets.store(payload.name, payload.value, session_factory)
        except backup_secrets.BackupSecretError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return status.payload()

    @app.post("/api/backup/generate-key")
    def generate_backup_key(request: Request) -> dict:
        """Make a backup key, store it, and return it once.

        The only endpoint that ever returns a secret value, and it does so because the
        administrator has to be able to put a copy somewhere off this machine. A backup key
        that exists only on the appliance the backups protect is not a backup key — so it
        is shown at the one moment there is something useful to do with it, and never
        again.
        """
        resolve_identity(request, admin=True)
        from knowledge_index.backup import secrets as backup_secrets

        key = backup_secrets.generate_key()
        status = backup_secrets.store(backup_secrets.ENCRYPTION_KEY, key, session_factory)
        return {
            "key": key,
            "fingerprint": status.fingerprint,
            "warning": (
                "This is the only time this key is shown. Save it somewhere that is not "
                "this appliance — a password manager, or the firm's secret store. Without "
                "it the backups cannot be opened by anyone, including us."
            ),
        }

    @app.post("/api/actions/backup-prune")
    def backup_prune(request: Request, payload: BackupPruneRequest | None = None) -> dict:
        """Apply the retention rules, or report what they would delete."""
        resolve_identity(request, admin=True)
        from knowledge_index.backup import runs as backup_runs

        return backup_runs.prune_backups(
            config_store.get(), dry_run=payload.dry_run if payload else True
        )

    @app.get("/api/runs")
    def runs(request: Request, limit: int = 50) -> list[dict]:
        resolve_identity(request, admin=True)
        _sweep_runs_if_due(session_factory, config_store.get())
        with session_factory() as session:
            rows = session.scalars(
                select(PipelineRunRecord)
                .order_by(PipelineRunRecord.created_at.desc())
                .limit(min(limit, 200))
            ).all()
            return [_run_payload(row) for row in rows]

    @app.get("/api/costs")
    def costs(request: Request) -> dict:
        """Realized spend from the UsageEvent ledger, written per gateway response.

        Tokens and calls are always measured; USD is only as good as the gateway's cost
        map, so a model the gateway cannot price is reported as unpriced rather than as
        free — a stage that consumed millions of tokens must never look like zero work."""
        identity = resolve_identity(request, admin=True)
        del identity
        with session_factory() as session:
            rows = session.scalars(
                select(UsageEvent).order_by(UsageEvent.created_at.desc()).limit(5000)
            ).all()

            def _bucket() -> dict[str, float | int]:
                return {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

            # Group by the model that actually ran, not by the gateway alias that was
            # called. Several aliases may point at one upstream model, so grouping by
            # alias claims more models than were billed and splits one model's spend
            # across rows — which the by-stage breakdown below already says, better.
            # Aliases that resolve to the same model collapse into one row; an alias the
            # gateway cannot resolve keeps its own name rather than being silently dropped.
            aliases = _gateway_model_aliases(config_store.get())
            by_model: dict[str, dict[str, float | int]] = defaultdict(_bucket)
            by_stage: dict[str, dict[str, float | int]] = defaultdict(_bucket)
            aliases_for_model: dict[str, set[str]] = defaultdict(set)
            for row in rows:
                model = aliases.get(row.model) or f"{row.provider}/{row.model}"
                aliases_for_model[model].add(row.model)
                for bucket in (by_model[model], by_stage[row.pipeline_stage or "unassigned"]):
                    bucket["cost_usd"] += row.cost_usd
                    bucket["input_tokens"] += row.input_tokens
                    bucket["output_tokens"] += row.output_tokens
                    bucket["calls"] += 1
            unpriced = sorted(
                model
                for model, values in by_model.items()
                if values["calls"] and not values["cost_usd"]
            )
            return {
                "total_usd": round(sum(row.cost_usd for row in rows), 4),
                "input_tokens": sum(row.input_tokens for row in rows),
                "output_tokens": sum(row.output_tokens for row in rows),
                "calls": len(rows),
                # Models the gateway has no rate for. The UI says so instead of
                # presenting $0.00 as a measured cost.
                "unpriced_models": unpriced,
                "by_model": [
                    {
                        "model": model,
                        # Which gateway aliases routed to it — the answer to "why is this
                        # model billed at all", without naming the row after a pipeline stage.
                        "aliases": sorted(aliases_for_model.get(model, ())),
                        **values,
                        "priced": bool(values["cost_usd"]),
                    }
                    for model, values in sorted(
                        by_model.items(),
                        key=lambda item: (item[1]["cost_usd"], item[1]["calls"]),
                        reverse=True,
                    )
                ],
                "by_stage": [
                    {"stage": stage, **values, "cost_usd": round(float(values["cost_usd"]), 4)}
                    for stage, values in sorted(
                        by_stage.items(),
                        key=lambda item: (item[1]["cost_usd"], item[1]["calls"]),
                        reverse=True,
                    )
                ],
            }

    @app.get("/api/audit")
    def audit_events(request: Request, limit: int = 50) -> list[dict]:
        resolve_identity(request, admin=True)
        with session_factory() as session:
            rows = session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(min(limit, 200))
            ).all()
            return [
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(),
                    "principals": row.actor_principals,
                    "action": row.action,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "outcome": row.outcome,
                    "details": row.details,
                }
                for row in rows
            ]

    @app.get("/api/quarantine")
    def quarantine_items(request: Request, limit: int = 50) -> list[dict]:
        resolve_identity(request, admin=True)
        with session_factory() as session:
            rows = session.execute(
                select(ProcessingState, SourceObject)
                .join(SourceObject, SourceObject.id == ProcessingState.source_object_id)
                .where(ProcessingState.status == "quarantined")
                .order_by(ProcessingState.updated_at.desc())
                .limit(min(limit, 200))
            ).all()
            return [
                {
                    "source_object_id": source_object.id,
                    "path": source_object.path,
                    "stage": state.stage,
                    "attempts": state.attempts,
                    "error": state.last_error,
                    "updated_at": state.updated_at.isoformat(),
                }
                for state, source_object in rows
            ]

    @app.post("/api/quarantine/{source_object_id}/retry")
    def retry_quarantined(
        source_object_id: str, request: Request, stage: str | None = None
    ) -> dict:
        """Release one quarantined document and start the run that picks it up.

        Without this a quarantine is permanent: nothing else in the pipeline reclaims a
        row that is neither done nor skipped, so a file that failed once against a model
        that was briefly unreachable stayed out of the index for good. The stage is queued
        again with a fresh attempt budget and every stage after it is invalidated, exactly
        as a producer-version bump does — a retry that left downstream results derived from
        the failed run in place would index a document from artifacts that no longer exist.
        """
        identity = resolve_identity(request, admin=True)
        config = config_store.get()
        try:
            outcome = PipelineRunner(session_factory, config).retry_quarantined(
                source_object_id, stage=stage
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if outcome is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no quarantined stage for source object {source_object_id}"
                    + (f" at stage {stage}" if stage else "")
                ),
            )
        with session_factory() as session:
            session.add(
                AuditEvent(
                    actor_principals=sorted(identity.principals),
                    action="quarantine.retry",
                    target_type="source_object",
                    target_id=source_object_id,
                    outcome="success",
                    # The failure that put it there is part of the record: an operator
                    # reviewing the log has to see what was overruled, not only that
                    # somebody pressed retry.
                    details={
                        "stage": outcome["stage"],
                        "invalidated_stages": outcome["invalidated_stages"],
                        "previous_error": outcome["previous_error"],
                    },
                )
            )
            session.commit()
        # A requeued row sits pending until a run claims it, and on this deployment no
        # run starts by itself. Returning "queued" without starting one would be the same
        # dead end in a different status.
        return {**outcome, "run": _launch_insertion(session_factory, config_store)}

    @app.get("/api/mcp/tools")
    async def mcp_tools(request: Request) -> list[dict]:
        """The tools this MCP server actually registered, in registration order.

        The console used to carry its own copy of this list and had fallen five tools
        behind, telling an administrator that external clients could do less than they
        can. A list maintained by hand drifts again the first time a tool is added, so it
        is read from the server instead. Administrator-only, like every other inventory of
        what the appliance exposes.
        """
        resolve_identity(request, admin=True)
        return [
            {
                "name": tool.name,
                # The description is model-facing prose; the console shows the short title.
                # A tool registered without one falls back to its first sentence, so a new
                # tool is legible rather than blank.
                "summary": tool.title or _first_sentence(tool.description or ""),
                "tags": sorted(tool.tags),
            }
            for tool in await mcp.list_tools()
        ]

    @app.get("/api/external-clients")
    def external_clients(request: Request) -> list[dict]:
        resolve_identity(request, admin=True)
        with session_factory() as session:
            return [
                _external_client_payload(row)
                for row in session.scalars(
                    select(ExternalClient).order_by(ExternalClient.name)
                ).all()
            ]

    @app.post("/api/external-clients", status_code=201)
    def create_external_client(payload: ExternalClientCreate, request: Request) -> dict:
        resolve_identity(request, admin=True)
        with session_factory() as session:
            row = ExternalClient(**payload.model_dump(), status="active")
            session.add(row)
            session.commit()
            return _external_client_payload(row)

    # ------------------------------------------------------------------- sign-in
    #
    # Everything under /api/identity writes the firm's Keycloak realm over its admin
    # REST API. The point is that an administrator never opens Keycloak: the admin
    # password and the provider's client secret stay in this process, and the browser
    # only ever sees what it typed.

    def _identity_admin() -> identity_admin.KeycloakAdmin:
        try:
            return identity_admin.KeycloakAdmin.from_config(config_store.get().identity)
        except identity_admin.IdentityAdminError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _record_identity_event(
        identity: Identity,
        action: str,
        alias: str,
        outcome: str,
        details: dict,
        target_type: str = "identity_provider",
    ) -> None:
        with session_factory() as session:
            session.add(
                AuditEvent(
                    actor_principals=sorted(identity.principals),
                    action=action,
                    target_type=target_type,
                    target_id=alias,
                    outcome=outcome,
                    details=details,
                )
            )
            session.commit()

    def _mirrored_identities(session: Session) -> tuple[dict[str, set[str]], set[str], dict[str, str]]:
        """Every person the connectors reported, and which sources reported them.

        The source half of the join that decides access. Both halves of it: directory
        membership from ``source_group_members`` and per-object shares from
        ``source_object_grants``, because a person can be reached either way.

        Only sources that name people can witness a match. A local folder mirrors no
        directory, so counting it would report a mismatch that does not exist.
        """
        sources = {row.id: row.display_name for row in session.scalars(select(Source)).all()}
        member_rows = session.execute(
            select(SourceGroupMember.source_id, SourceGroupMember.member_id)
            .where(SourceGroupMember.member_type == "user")
            .distinct()
        ).all()
        grant_rows = session.execute(
            select(SourceObject.source_id, SourceObjectGrant.principal)
            .join(SourceObject, SourceObject.id == SourceObjectGrant.source_object_id)
            .where(SourceObjectGrant.principal_kind == "user")
            .distinct()
        ).all()
        index: dict[str, set[str]] = defaultdict(set)
        witnesses: set[str] = set()
        for source_id, member_id in member_rows:
            index[str(member_id).casefold()].add(source_id)
            witnesses.add(source_id)
        for source_id, principal in grant_rows:
            index[str(principal).casefold().removeprefix("user:")].add(source_id)
            witnesses.add(source_id)
        return index, witnesses, sources

    def _is_self(caller: Identity, user: dict) -> bool:
        """Is this realm account the one making the request?

        Checked against every name the caller could be carrying, because the answer
        differs by deployment: behind an OIDC login the subject *is* the Keycloak user
        id, behind a trusted proxy it is the address the proxy asserted. Matching any
        of them is enough to refuse — the cost of being wrong in the other direction is
        an administrator who has locked themselves out of their own appliance.
        """
        account = {
            str(user.get(key) or "").casefold() for key in ("id", "username", "email")
        } - {""}
        held = {caller.subject.casefold(), caller.username.casefold()} | {
            item.partition(":")[2]
            for item in caller.principals
            if item.startswith(("user:", "username:"))
        }
        return bool(account & (held - {""}))

    def _match_report(email: str) -> dict:
        """Whether the sources already know this address — the answer before creating.

        An account whose address no connector reported is not broken and raises
        nothing; it simply opens onto an empty index. Said at creation time it is a
        typo caught in a second. Found afterwards it is a support call.
        """
        with session_factory() as session:
            index, witnesses, sources = _mirrored_identities(session)
        matched = sorted(index.get(email.casefold(), set()))
        return {
            "matched_sources": [sources.get(item, item) for item in matched],
            "unmatched_sources": sorted(
                sources.get(item, item) for item in witnesses - set(matched)
            ),
            "sources_reporting_identities": sorted(
                sources.get(item, item) for item in witnesses
            ),
        }

    @app.get("/api/identity/providers")
    def sign_in_providers(request: Request) -> dict:
        """The sign-in choices, what is configured, and the URI to register where.

        The redirect URI is returned even for providers nobody has configured: it is
        the value a firm has to register at Google or Entra *before* it has anything
        to paste back here, and getting it wrong is the single most common reason a
        first login fails.
        """
        resolve_identity(request, admin=True)
        config = config_store.get()
        states: dict[str, identity_admin.ProviderState] = {
            item.kind: identity_admin.ProviderState(
                kind=item.kind,
                alias=item.kind,
                redirect_uri=config.identity.broker_redirect_uri(item.kind),
            )
            for item in identity_admin.PRESETS.values()
        }
        with session_factory() as session:
            stored = {row.alias: row for row in session.scalars(select(IdentityProviderCredential)).all()}
        realm_error = ""
        instances: dict[str, dict] = {}
        claims: list[dict] = []
        try:
            with _identity_admin() as admin:
                instances = {item.get("alias"): item for item in admin.identity_providers()}
                claims = [check.payload() for check in admin.token_claim_state(config.security.oidc_audience)]
        except HTTPException as exc:
            realm_error = str(exc.detail)
        except identity_admin.IdentityAdminError as exc:
            realm_error = str(exc)
        for alias, instance in instances.items():
            state = states.get(alias)
            if state is None:  # configured by hand in Keycloak before this page existed
                state = identity_admin.ProviderState(
                    kind="oidc", alias=alias, redirect_uri=config.identity.broker_redirect_uri(alias)
                )
                states[alias] = state
            state.configured = True
            state.enabled = bool(instance.get("enabled"))
            state.display_name = instance.get("displayName") or alias
            state.client_id = (instance.get("config") or {}).get("clientId", "")
            state.issuer = (instance.get("config") or {}).get("issuer", "")
        for alias, row in stored.items():
            state = states.get(alias)
            if state is None:
                continue
            state.discovery_url = row.discovery_url
            state.extra_value = row.extra_value or ""
            state.last_tested_at = row.last_tested_at.isoformat() if row.last_tested_at else None
            state.last_test_ok = row.last_test_ok
            state.checks = (row.last_test_detail or {}).get("checks", [])
        return {
            "catalog": identity_admin.catalog(),
            "providers": [states[key].payload() for key in sorted(states)],
            "realm": config.identity.realm,
            "auth_mode": config.security.auth_mode,
            "realm_error": realm_error,
            "token_claims": claims,
        }

    @app.post("/api/identity/providers", status_code=201)
    def configure_sign_in_provider(payload: SignInProviderCreate, request: Request) -> dict:
        """Write one identity provider into the realm, and everything it needs to work.

        In order: validate the provider's discovery document, prove the client id and
        secret against its token endpoint, then create the broker, the username mapper
        that makes the imported account match what connectors report, and the token
        claims the appliance validates. Nothing is stored if the provider does not
        recognise the credentials — a broker that cannot log anybody in is worse than
        no broker, because it appears on the login page.
        """
        identity = resolve_identity(request, admin=True)
        config = config_store.get()
        spec = identity_admin.preset(payload.kind)
        alias = spec.kind
        redirect_uri = config.identity.broker_redirect_uri(alias)
        try:
            discovery_url = spec.discovery_url(payload.extra)
            document = identity_admin.fetch_discovery(discovery_url)
        except identity_admin.IdentityAdminError as exc:
            _record_identity_event(identity, "identity.provider.configure", alias, "error", {"stage": "discovery", "reason": str(exc)})
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        credential_check = identity_admin.probe_client_credentials(
            document["token_endpoint"], payload.client_id, payload.client_secret, redirect_uri
        )
        if not credential_check.ok:
            _record_identity_event(identity, "identity.provider.configure", alias, "denied", {"stage": "credentials", "reason": credential_check.detail})
            raise HTTPException(status_code=400, detail=f"{spec.label} rejected the client id or secret: {credential_check.detail}")

        with _identity_admin() as admin:
            try:
                action = admin.upsert_identity_provider(
                    identity_admin.identity_provider_payload(
                        alias,
                        display_name=payload.display_name or spec.label,
                        client_id=payload.client_id,
                        client_secret=payload.client_secret,
                        discovery=document,
                        scopes=spec.scopes,
                    )
                )
                mappers = admin.ensure_broker_mappers(alias)
                claims = admin.ensure_token_claims(config.security.oidc_audience)
            except identity_admin.IdentityAdminError as exc:
                _record_identity_event(identity, "identity.provider.configure", alias, "error", {"stage": "realm", "reason": str(exc)})
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        with session_factory() as session:
            row = session.get(IdentityProviderCredential, alias) or IdentityProviderCredential(alias=alias)
            row.kind = spec.kind
            row.client_id = payload.client_id
            row.payload = encrypt_credentials({"client_secret": payload.client_secret})
            row.key_fingerprint = key_fingerprint()
            row.discovery_url = discovery_url
            row.extra_value = payload.extra.strip() or None
            row.issuer = document["issuer"]
            session.merge(row)
            session.commit()

        _record_identity_event(
            identity,
            "identity.provider.configure",
            alias,
            "success",
            {"action": action, "issuer": document["issuer"], "mappers": mappers, "realm_changes": claims},
        )
        return {"alias": alias, "action": action, "redirect_uri": redirect_uri, "issuer": document["issuer"], "realm_changes": claims}

    @app.delete("/api/identity/providers/{alias}")
    def remove_sign_in_provider(alias: str, request: Request) -> dict:
        identity = resolve_identity(request, admin=True)
        with _identity_admin() as admin:
            try:
                removed = admin.delete_identity_provider(alias)
            except identity_admin.IdentityAdminError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        with session_factory() as session:
            session.execute(
                delete(IdentityProviderCredential).where(IdentityProviderCredential.alias == alias)
            )
            session.commit()
        _record_identity_event(identity, "identity.provider.remove", alias, "success", {"was_present": removed})
        return {"alias": alias, "removed": removed}

    @app.post("/api/identity/providers/{alias}/test")
    def test_sign_in(alias: str, request: Request) -> dict:
        """Five things that have to hold before a lawyer can sign in, each really checked.

        Nothing here is inferred from what was saved: the discovery document is
        re-fetched, the stored client secret is re-presented to the provider's token
        endpoint, the broker is re-read out of the realm, and the login is started the
        way a browser starts it to see whether Keycloak actually hands off.
        """
        identity = resolve_identity(request, admin=True)
        config = config_store.get()
        with session_factory() as session:
            row = session.get(IdentityProviderCredential, alias)
            if row is None:
                raise HTTPException(status_code=404, detail=f"no sign-in provider is configured as {alias!r}")
            client_id, discovery_url = row.client_id, row.discovery_url
            secret = decrypt_credentials(row.payload).get("client_secret", "")

        checks: list[identity_admin.Check] = []
        document: dict = {}
        try:
            document = identity_admin.fetch_discovery(discovery_url)
            checks.append(identity_admin.Check("discovery", "Provider discovery document", True, f"issuer {document['issuer']}"))
        except identity_admin.IdentityAdminError as exc:
            checks.append(identity_admin.Check("discovery", "Provider discovery document", False, str(exc)))

        if document:
            checks.append(
                identity_admin.probe_client_credentials(
                    document["token_endpoint"], client_id, secret, config.identity.broker_redirect_uri(alias)
                )
            )

        with _identity_admin() as admin:
            try:
                instance = admin.identity_provider(alias)
            except identity_admin.IdentityAdminError as exc:
                instance = None
                checks.append(identity_admin.Check("realm", "Provider present in the realm", False, str(exc)))
            if instance is not None:
                configured_issuer = (instance.get("config") or {}).get("issuer", "")
                agrees = not document or configured_issuer == document["issuer"]
                enabled = bool(instance.get("enabled"))
                checks.append(
                    identity_admin.Check(
                        "realm",
                        "Provider present in the realm",
                        enabled and agrees,
                        "enabled"
                        if enabled and agrees
                        else "present but disabled, so it never appears on the login page"
                        if enabled is False and agrees
                        else f"the realm points at {configured_issuer}, the provider says {document['issuer']}",
                    )
                )
            elif not any(check.id == "realm" for check in checks):
                checks.append(identity_admin.Check("realm", "Provider present in the realm", False, "not found in the realm"))
            try:
                checks.extend(admin.token_claim_state(config.security.oidc_audience))
                if document:
                    checks.append(
                        admin.broker_login_redirect(
                            alias, config.identity.audience_client_id, document["authorization_endpoint"]
                        )
                    )
            except identity_admin.IdentityAdminError as exc:
                checks.append(identity_admin.Check("login", "Keycloak hands off to the provider", False, str(exc)))

        payload = {"alias": alias, "ok": all(check.ok for check in checks), "checks": [check.payload() for check in checks]}
        with session_factory() as session:
            row = session.get(IdentityProviderCredential, alias)
            if row is not None:
                row.last_tested_at = datetime.now(UTC)
                row.last_test_ok = payload["ok"]
                row.last_test_detail = {"checks": payload["checks"]}
                session.commit()
        _record_identity_event(
            identity,
            "identity.provider.test",
            alias,
            "success" if payload["ok"] else "error",
            {"failed": [check["id"] for check in payload["checks"] if not check["ok"]]},
        )
        return payload

    @app.get("/api/identity/people")
    def sign_in_people(request: Request, limit: int = 200) -> dict:
        """Who can sign in, and whether the connectors know them by the same name.

        Access is decided by matching the address a login asserts against the address a
        connector mirrored. When those differ the person sees nothing from that source
        and no error is raised anywhere, so the mismatch is only ever visible if
        something puts the two lists side by side. This does.

        Derived entirely from mirrored data: ``source_group_members`` for directory
        membership and ``source_object_grants`` for per-object shares. A source that
        reports no identities at all is not counted against anybody.
        """
        caller = resolve_identity(request, admin=True)
        config = config_store.get()
        aliases = {
            key.strip().casefold(): value.strip().casefold()
            for key, value in (config.security.principal_aliases or {}).items()
        }
        realm_error = ""
        realm_users: list[dict] = []
        links: dict[str, list[str]] = {}
        try:
            with _identity_admin() as admin:
                realm_users = admin.users(limit=limit)
                links = {
                    user["id"]: [item.get("identityProvider", "") for item in admin.user_identity_links(user["id"])]
                    for user in realm_users
                }
        except HTTPException as exc:
            realm_error = str(exc.detail)
        except identity_admin.IdentityAdminError as exc:
            realm_error = str(exc)

        with session_factory() as session:
            identity_index, witnesses, sources = _mirrored_identities(session)
            # Last time each principal actually reached this appliance. Scanned in
            # Python because actor_principals is a JSON array on both backends and the
            # window that matters is small.
            seen: dict[str, str] = {}
            for row in session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(2000)
            ).all():
                for principal in row.actor_principals or []:
                    value = str(principal).casefold()
                    if value.startswith(("user:", "username:")):
                        seen.setdefault(value.partition(":")[2], row.created_at.isoformat())

        witness_names = sorted(sources.get(source_id, source_id) for source_id in witnesses)
        people = []
        for user in realm_users:
            username = str(user.get("username") or "").casefold()
            email = str(user.get("email") or "").casefold()
            # Aliases are the fix for exactly this mismatch, so a person an admin has
            # already bridged must read as matched.
            candidates = {value for value in (username, email) if value}
            candidates |= {aliases[f"user:{value}"].removeprefix("user:") for value in list(candidates) if f"user:{value}" in aliases}
            matched = sorted({source_id for value in candidates for source_id in identity_index.get(value, set())})
            people.append(
                {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "email": user.get("email"),
                    "name": " ".join(filter(None, [user.get("firstName"), user.get("lastName")])).strip(),
                    "enabled": bool(user.get("enabled")),
                    "federated": sorted({item for item in links.get(user.get("id"), []) if item}),
                    "last_seen": seen.get(email) or seen.get(username),
                    "matched_sources": [sources.get(source_id, source_id) for source_id in matched],
                    "unmatched_sources": sorted(
                        sources.get(source_id, source_id) for source_id in witnesses - set(matched)
                    ),
                    "alias": aliases.get(f"user:{email}") or aliases.get(f"user:{username}"),
                    # Marks the row the caller is standing in, so the console can refuse
                    # to offer them the button that locks them out.
                    "is_self": _is_self(caller, user),
                }
            )
        people.sort(key=lambda item: (len(item["matched_sources"]), item["username"] or ""))
        return {
            "people": people,
            "sources_reporting_identities": witness_names,
            "source_identities": sorted(identity_index),
            # The same index keyed by address, so the console can answer "does anything
            # know this person?" while an administrator is still typing it.
            "source_identity_sources": {
                value: sorted(sources.get(item, item) for item in found)
                for value, found in identity_index.items()
            },
            "aliases": config.security.principal_aliases or {},
            "realm_error": realm_error,
        }

    # ------------------------------------------------------------- local accounts
    #
    # Not every firm has a directory. A two-partner practice with a SharePoint site and
    # no SSO still needs people who can sign in, and the only other way to make one is
    # Keycloak's own console — which is the thing this page exists to replace.
    #
    # The password is generated here, handed to Keycloak, returned once, and forgotten.
    # Knowledge Index never stores it, never logs it, and no endpoint can retrieve it.

    def _issued_password(user_id: str, admin: identity_admin.KeycloakAdmin) -> str:
        password = identity_admin.generate_password()
        admin.set_password(user_id, password, temporary=True)
        admin.require_password_change(user_id)
        return password

    @app.post("/api/identity/people", status_code=201)
    def create_local_person(payload: LocalPersonCreate, request: Request) -> dict:
        """Create someone who signs in here with a password.

        The email is the identity, and the realm username is set to it, because that
        string is what every connector's mirrored membership is matched against. The
        response says whether any source actually reported this address: an account
        that matches nothing works perfectly and shows nothing, and that is worth
        saying while the administrator is still looking at the form.
        """
        identity = resolve_identity(request, admin=True)
        try:
            email = identity_admin.normalize_email(payload.email)
        except identity_admin.IdentityAdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with _identity_admin() as admin:
            try:
                admin.ensure_password_policy(identity_admin.LOCAL_PASSWORD_POLICY)
                user = admin.create_user(
                    email, first_name=payload.first_name, last_name=payload.last_name
                )
                password = _issued_password(str(user["id"]), admin)
            except identity_admin.IdentityAdminError as exc:
                _record_identity_event(
                    identity, "identity.person.create", email, "error", {"reason": str(exc)}, "identity_user"
                )
                # "can already sign in" is the caller's mistake, not the realm's.
                status = 409 if "already sign in" in str(exc) else 502
                raise HTTPException(status_code=status, detail=str(exc)) from exc
        match = _match_report(email)
        _record_identity_event(
            identity,
            "identity.person.create",
            email,
            "success",
            # The password is not in here, and must never be.
            {"user_id": user["id"], "matched_sources": match["matched_sources"]},
            "identity_user",
        )
        return {
            "id": user["id"],
            "username": user["username"],
            "email": email,
            # Shown once. There is no second way to read it — a reset issues a new one.
            "temporary_password": password,
            **match,
        }

    @app.post("/api/identity/people/{user_id}/password")
    def reset_local_password(user_id: str, request: Request) -> dict:
        """Issue a new temporary password. The old one stops working immediately."""
        identity = resolve_identity(request, admin=True)
        with _identity_admin() as admin:
            try:
                user = admin.user(user_id)
                if user is None:
                    raise HTTPException(status_code=404, detail="that person is not in the realm")
                admin.ensure_password_policy(identity_admin.LOCAL_PASSWORD_POLICY)
                password = _issued_password(user_id, admin)
            except identity_admin.IdentityAdminError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        _record_identity_event(
            identity, "identity.person.reset_password", str(user.get("username") or user_id), "success", {}, "identity_user"
        )
        return {"id": user_id, "username": user.get("username"), "temporary_password": password}

    @app.post("/api/identity/people/{user_id}/enabled")
    def set_local_person_enabled(user_id: str, payload: PersonEnabledUpdate, request: Request) -> dict:
        """Turn an account off without losing it, or turn it back on."""
        identity = resolve_identity(request, admin=True)
        with _identity_admin() as admin:
            try:
                user = admin.user(user_id)
                if user is None:
                    raise HTTPException(status_code=404, detail="that person is not in the realm")
                if not payload.enabled and _is_self(identity, user):
                    raise HTTPException(
                        status_code=400, detail="you cannot disable the account you are signed in with"
                    )
                admin.set_user_enabled(user_id, payload.enabled)
            except identity_admin.IdentityAdminError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        _record_identity_event(
            identity,
            "identity.person.enabled" if payload.enabled else "identity.person.disabled",
            str(user.get("username") or user_id),
            "success",
            {},
            "identity_user",
        )
        return {"id": user_id, "username": user.get("username"), "enabled": payload.enabled}

    @app.delete("/api/identity/people/{user_id}")
    def delete_local_person(user_id: str, request: Request) -> dict:
        """Remove the account entirely. Disabling is the reversible one."""
        identity = resolve_identity(request, admin=True)
        with _identity_admin() as admin:
            try:
                user = admin.user(user_id)
                if user is None:
                    raise HTTPException(status_code=404, detail="that person is not in the realm")
                if _is_self(identity, user):
                    raise HTTPException(
                        status_code=400, detail="you cannot delete the account you are signed in with"
                    )
                admin.delete_user(user_id)
            except identity_admin.IdentityAdminError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        _record_identity_event(
            identity, "identity.person.delete", str(user.get("username") or user_id), "success", {}, "identity_user"
        )
        return {"id": user_id, "username": user.get("username"), "deleted": True}

    @app.post("/api/identity/aliases")
    def add_principal_alias(payload: PrincipalAliasCreate, request: Request) -> dict:
        """Bridge a sign-in identity onto the one a source reported for the same person.

        Additive and never a denial: the alias only adds principals a caller holds, so
        the worst a wrong entry does is fail to match.
        """
        identity = resolve_identity(request, admin=True)
        principal = payload.principal.strip().casefold()
        alias = payload.alias.strip().casefold()
        if principal == alias:
            raise HTTPException(status_code=400, detail="a principal cannot be an alias of itself")
        config = config_store.get()
        config.security.principal_aliases = {**config.security.principal_aliases, principal: alias}
        config_store.save(config)
        configure_access(
            source_acl_mode=config.security.source_acl_mode,
            principal_aliases=config.security.principal_aliases,
        )
        _record_identity_event(identity, "identity.alias.add", principal, "success", {"alias": alias})
        return {"principal": principal, "alias": alias, "aliases": config.security.principal_aliases}

    @app.delete("/api/identity/aliases")
    def remove_principal_alias(request: Request, principal: str) -> dict:
        identity = resolve_identity(request, admin=True)
        config = config_store.get()
        remaining = {
            key: value
            for key, value in config.security.principal_aliases.items()
            if key.strip().casefold() != principal.strip().casefold()
        }
        config.security.principal_aliases = remaining
        config_store.save(config)
        configure_access(
            source_acl_mode=config.security.source_acl_mode, principal_aliases=remaining
        )
        _record_identity_event(identity, "identity.alias.remove", principal, "success", {})
        return {"aliases": remaining}

    return app


_ASK_PLANNER_SYSTEM = (
    "You plan retrieval for a permission-scoped legal knowledge index. Choose the "
    "retrieval tools that best answer the user's question. Available tools:\n"
    "- search_semantic(query, filters): dense + lexical hybrid search over document "
    "chunks. Use for conceptual questions. `query` is required.\n"
    "- search_filter(filters): list documents matching structured filters only, no "
    "query. Use when the user asks for documents of a type/status/matter.\n"
    "- search_decisions(query): search extracted drafting-decision rationale records. "
    "Use for 'why was this changed / negotiated' questions. `query` is required.\n"
    "- traverse(entity_type, entity_id): follow ontology relations from a known "
    "entity (document, matter, thread). Only use when the question already names a "
    "concrete entity id.\n"
    "Filters may set project_id, matter_id, doc_type, version_status, language. "
    "Prefer one or two focused steps. Do not invent entity ids."
)

_ASK_SYNTHESIS_SYSTEM = (
    "You answer strictly from the provided evidence. Rules:\n"
    "- Use ONLY the documents in the evidence JSON. Never rely on outside knowledge.\n"
    "- Answer in the same language as the question.\n"
    "- Cite only documents that appear in the evidence, using their document_id and "
    "title, with a short verbatim quote from that evidence.\n"
    "- If the evidence does not contain enough to answer, say so explicitly in the "
    "answer and return no unsupported claims."
)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _run_ask_plan(
    service: RetrievalService,
    plan: Plan,
    principals: set[str],
    request_project_id: str | None,
) -> list[dict]:
    """Execute each planned retrieval call under the caller's principals and gather
    normalized evidence rows. The request project_id, when present, is authoritative."""

    evidence: list[dict] = []
    seen: set[str] = set()
    for step in plan.steps:
        filter_data = step.filters.model_dump()
        if request_project_id:
            filter_data["project_id"] = request_project_id
        filters = SearchFilters(**filter_data)
        if step.tool == "search_semantic" and (step.query or "").strip():
            for hit in service.search_semantic(
                step.query or "", principals=principals, filters=filters, limit=8
            ):
                _add_hit_evidence(evidence, seen, hit)
        elif step.tool == "search_filter":
            for hit in service.search_filter(principals=principals, filters=filters, limit=8):
                _add_hit_evidence(evidence, seen, hit)
        elif step.tool == "search_decisions" and (step.query or "").strip():
            for row in service.search_decisions(
                step.query or "", principals=principals, limit=8
            ):
                key = f"decision:{row['id']}"
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    {
                        "kind": "decision",
                        "document_id": row.get("document_id"),
                        "title": row.get("locus") or "decision record",
                        "quote": row.get("rationale_text") or row.get("change_summary") or "",
                    }
                )
        elif step.tool == "traverse" and step.entity_type and step.entity_id:
            for relation in service.traverse(
                step.entity_type, step.entity_id, principals=principals, limit=20
            ):
                key = f"rel:{relation['from']['id']}:{relation['kind']}:{relation['to']['id']}"
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    {
                        "kind": "relation",
                        "document_id": relation["to"]["id"],
                        "title": f"{relation['from']['type']} {relation['kind']} {relation['to']['type']}",
                        "quote": relation["kind"],
                    }
                )
    return evidence


def _add_hit_evidence(evidence: list[dict], seen: set[str], hit: Any) -> None:
    key = f"doc:{hit.document_id}"
    if key in seen:
        return
    seen.add(key)
    evidence.append(
        {
            "kind": "document",
            "document_id": hit.document_id,
            "title": hit.title,
            "doc_type": hit.doc_type,
            "version_status": hit.version_status,
            "quote": hit.excerpt,
        }
    )


def _gateway_model_aliases(config) -> dict[str, str]:
    """Map each gateway alias to the model it actually calls.

    Spend is recorded against the alias an assignment names, because that is all the gateway
    reports back: `x-litellm-model-group` is the alias and `x-litellm-model-id` is an
    opaque deployment hash. Resolving here rather than at write time keeps the ledger
    free of a per-call gateway lookup, and an unreachable gateway costs a nicer label,
    never a number.
    """
    try:
        response = httpx.get(
            f"{gateway_url(config)}/model/info", headers=gateway_admin_headers(), timeout=5
        )
        response.raise_for_status()
    except Exception:  # noqa: BLE001 - cosmetic resolution; spend must still be reported
        return {}
    resolved: dict[str, str] = {}
    for item in response.json().get("data", []):
        alias = str(item.get("model_name") or "")
        upstream = str((item.get("litellm_params") or {}).get("model") or "")
        if alias and upstream:
            resolved[alias] = upstream
    return resolved


def _gateway_model_entry(item: dict) -> dict:
    """One gateway model, reduced to what the admin UI may see.

    ``litellm_params`` is dropped wholesale rather than filtered: it is the structure
    that carries provider keys, and an allow-list is the only safe direction."""
    params = item.get("litellm_params") or {}
    info = item.get("model_info") or {}
    return {
        "id": str(item.get("model_name") or ""),
        "upstream_model": str(params.get("model") or ""),
        "api_base": params.get("api_base"),
        "mode": info.get("mode"),
        # Config-file models cannot be edited or removed from the UI; runtime ones can.
        "source": "runtime" if info.get("db_model") else "config",
    }


def _gateway_credentials(base: str, headers: dict[str, str]) -> list[dict]:
    """Provider credential *names* the gateway holds. Values never cross this boundary."""
    response = httpx.get(f"{base}/credentials", headers=headers, timeout=8)
    response.raise_for_status()
    return [
        {
            "name": str(entry.get("credential_name") or ""),
            "description": (entry.get("credential_info") or {}).get("description"),
        }
        for entry in response.json().get("credentials", [])
        if entry.get("credential_name")
    ]


def _gateway_error(response: httpx.Response) -> str:
    """The gateway's own words. Guessing at a cause would hide the real one."""
    try:
        detail = response.json().get("error") or response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, dict):
        detail = detail.get("message") or detail
    return str(detail or response.text)[:1000]


async def _probe(client: httpx.AsyncClient, url: str | None) -> str:
    """Live reachability, not configuration: any HTTP answer (even 401/404) counts."""
    if not url:
        return "disabled"
    try:
        await client.get(url)
        return "ok"
    except httpx.HTTPError:
        return "unreachable"


def _project_payload(session: Session, project: Project, identity: Identity) -> dict:
    return {
        "id": project.id,
        "key": project.key,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "documents": session.scalar(
            select(func.count()).select_from(Document).where(Document.project_id == project.id)
        )
        or 0,
        "sources": session.scalar(
            select(func.count()).select_from(Source).where(Source.project_id == project.id)
        )
        or 0,
        "can_manage": AccessService(session).can_manage_project(
            project.id, set(identity.principals)
        ),
    }


def _grant_payload(row: ProjectGrant | DocumentGrant) -> dict:
    return {
        "id": row.id,
        "principal": row.principal,
        "principal_kind": row.principal_kind,
        "effect": row.effect,
        "role": row.role,
        "origin": row.origin,
        "external_id": row.external_id,
    }


def _document_payloads(
    session: Session, documents: list[Document], principals: set[str]
) -> list[dict]:
    """Serialize a document page with a fixed number of aggregate queries."""

    if not documents:
        return []
    document_ids = [document.id for document in documents]
    versions = session.scalars(
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.id.in_(document_ids),
            AccessService(session).version_predicate(principals),
        )
        .order_by(
            DocumentVersion.document_id,
            DocumentVersion.ordinal.desc().nullslast(),
            DocumentVersion.created_at.desc(),
        )
    ).all()
    versions_by_document: dict[str, list[DocumentVersion]] = defaultdict(list)
    for version in versions:
        versions_by_document[version.document_id].append(version)
    version_ids = [version.id for version in versions]
    chunk_counts = (
        dict(
            session.execute(
                select(DocumentVersion.document_id, func.count(Chunk.id))
                .join(Chunk, Chunk.document_version_id == DocumentVersion.id)
                .where(DocumentVersion.id.in_(version_ids))
                .group_by(DocumentVersion.document_id)
            ).all()
        )
        if version_ids
        else {}
    )
    project_ids = {document.project_id for document in documents if document.project_id}
    matter_ids = {document.matter_id for document in documents if document.matter_id}
    projects = {
        project.id: project
        for project in session.scalars(select(Project).where(Project.id.in_(project_ids))).all()
    }
    matters = {
        matter.id: matter
        for matter in session.scalars(select(Matter).where(Matter.id.in_(matter_ids))).all()
    }

    payloads: list[dict] = []
    for document in documents:
        document_versions = versions_by_document.get(document.id, [])
        latest = document_versions[0] if document_versions else None
        project = projects.get(document.project_id)
        matter = matters.get(document.matter_id)
        payloads.append(
            {
                "id": document.id,
                "project_id": document.project_id,
                "matter_id": document.matter_id,
                "title": document.title,
                "doc_type": document.doc_type,
                "language": document.language,
                "doc_date": document.doc_date.isoformat() if document.doc_date else None,
                "parties": document.parties,
                "identifiers": document.identifiers,
                "versions": len(document_versions),
                "chunks": int(chunk_counts.get(document.id, 0)),
                "latest_status": latest.status if latest else "unknown",
                "latest_version_id": latest.id if latest else None,
                "latest_content_hash": latest.content_hash if latest else None,
                "project": (
                    {"id": project.id, "key": project.key, "name": project.name}
                    if project
                    else None
                ),
                "matter": (
                    {
                        "id": matter.id,
                        "title": matter.title,
                        "reference_numbers": matter.reference_numbers,
                        "practice_area": matter.practice_area,
                        "status": matter.status,
                    }
                    if matter
                    else None
                ),
                "created_at": document.created_at.isoformat(),
                "updated_at": document.updated_at.isoformat(),
            }
        )
    return payloads


def _document_payload(session: Session, document: Document, principals: set[str]) -> dict:
    """Compatibility wrapper for callers that serialize one document."""

    return _document_payloads(session, [document], principals)[0]


def _default_browse_root() -> Path:
    """A sensible starting directory for the folder picker, from what the server can see."""
    for candidate in ("/Users", "/home", "/data", "/"):
        if Path(candidate).is_dir():
            return Path(candidate)
    return Path("/")


def _principal_kind_hint(principal: str) -> str:
    """Best-effort principal kind from its prefix when a stored kind is missing."""
    prefix = principal.split(":", 1)[0].lower() if ":" in principal else ""
    return prefix if prefix in {"user", "group", "service", "role"} else "group"


def _ontology_label(config: AppConfig, node_id: str | None) -> str | None:
    """The human name behind an ontology id.

    doc_type stores the ontology's own identifier — "RDMmVnDBUmOnVx8i4ZpOt2G" — because
    that is what stays stable when a label is reworded. Shown to a lawyer it is noise, so
    it is resolved here rather than in the browser: the ontology artifact is tens of
    megabytes and already open in this process.
    """
    if not node_id:
        return None
    try:
        return config.ontology_facet("doc_type").label_of(node_id)
    except Exception:  # noqa: BLE001 - an unplugged or changed ontology is not an error here
        return None


def _ontology_path(config: AppConfig, node_id: str | None) -> list[str]:
    """Root-to-leaf labels for one id, which is the classifier's reasoning made readable."""
    if not node_id:
        return []
    try:
        return config.ontology_facet("doc_type").path_labels(node_id)
    except Exception:  # noqa: BLE001 - same
        return []


def _bump_version(current: str | None) -> str:
    """Deterministically advance a re-run token so requeue picks up a rebuild."""
    base = current or "0"
    head, _, tail = base.rpartition("-")
    if head and tail.isdigit():
        return f"{head}-{int(tail) + 1}"
    return f"{base}-r2"


def _first_sentence(text: str) -> str:
    """First sentence of a tool description, for a console that shows one line per tool."""
    head = text.strip().split(". ")[0].strip()
    return head if head.endswith(".") or not head else f"{head}."


def _launch_insertion(session_factory: sessionmaker, config_store: ConfigStore) -> dict:
    """Start one insertion run through the configured orchestrator (hatchet or local).

    The trigger itself lives in ``knowledge_index.orchestration`` because the folder
    watcher and the sync handoff start the same pipeline without going through HTTP;
    this only translates its failures into status codes."""
    from knowledge_index.orchestration.insertion import OrchestratorUnavailable, launch_insertion

    config = config_store.get()
    try:
        return launch_insertion(session_factory, config)
    except OrchestratorUnavailable as exc:
        if config.components.orchestrator_provider not in ("local", "hatchet"):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _validate_rubric(items: list[dict]) -> list[dict]:
    from knowledge_index.pipeline.extraction import RubricItem

    validated: list[dict] = []
    for index, item in enumerate(items):
        data = dict(item)
        data.setdefault("id", f"r{index + 1}")
        try:
            validated.append(RubricItem.model_validate(data).model_dump())
        except Exception as exc:  # surface a precise 422, don't 500
            raise HTTPException(
                status_code=422, detail=f"invalid rubric item {index}: {exc}"
            ) from exc
    return validated


def _validate_verifiers(items: list[dict]) -> list[dict]:
    from knowledge_index.pipeline.extraction import EnvVerifier

    validated: list[dict] = []
    for index, item in enumerate(items):
        try:
            validated.append(EnvVerifier.model_validate(item).model_dump())
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid verifier {index}: {exc}") from exc
    return validated


def _environment_payload(session: Session, record: EvalRecord) -> dict:
    def _version_label(version_id: str | None) -> dict:
        version = session.get(DocumentVersion, version_id) if version_id else None
        if version is None:
            return {"version_id": version_id, "title": None}
        document = session.get(Document, version.document_id)
        return {
            "version_id": version_id,
            "document_id": version.document_id,
            "title": document.title if document else None,
            "status": version.status,
        }

    matter = session.get(Matter, record.matter_id) if record.matter_id else None
    return {
        "id": record.id,
        "status": record.status,
        "task_type": record.task_type,
        "instruction": record.instruction,
        "practice_area": record.practice_area,
        "authored_internally": record.authored_internally,
        "holdout": record.holdout,
        "matter": (
            {"id": matter.id, "title": matter.title, "reference_numbers": matter.reference_numbers}
            if matter
            else None
        ),
        "reference_output": _version_label(record.reference_output_ref),
        "input_files": [_version_label(ref) for ref in (record.input_refs or [])],
        "rubric": record.rubric or [],
        "verifiers": record.verifiers or [],
        "approved_by": record.approved_by,
        "approved_at": record.approved_at.isoformat() if record.approved_at else None,
        "confidence": (record.provenance or {}).get("confidence"),
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _decide_environment(
    session_factory: sessionmaker, env_id: str, status: str, identity: Identity
) -> dict:
    with session_factory() as session:
        record = session.get(EvalRecord, env_id)
        if record is None:
            raise HTTPException(status_code=404, detail="environment not found")
        record.status = status
        if status == "approved":
            record.approved_by = identity.username or (
                sorted(identity.principals)[0] if identity.principals else None
            )
            record.approved_at = datetime.now(UTC)
        else:
            record.approved_by = None
            record.approved_at = None
        session.commit()
        return _environment_payload(session, record)


def _source_payload(session: Session, source: Source, config: AppConfig) -> dict:
    from knowledge_index.connectors.events import event_delivery_payload

    return {
        "id": source.id,
        "project_id": source.project_id,
        "kind": source.kind,
        "display_name": source.display_name,
        "status": source.status,
        "provider": source.provider,
        "provider_connection_id": source.provider_connection_id,
        "source_connection_id": (source.config or {}).get("source_connection_id"),
        "sync_policy": source.sync_policy,
        # Event delivery and policy are complementary: events provide latency, while the
        # policy interval is the reconciliation safety net. Showing both prevents
        # "continuous" from looking like either a webhook or a timer depending on kind.
        "event_delivery": event_delivery_payload(session, source, config),
        # What this connection actually syncs, and whether its permissions are mirrored —
        # both are things an operator has to be able to see without reading the database.
        "scope": _source_scope(source),
        "mirrors_acls": _source_mirrors_acls(source),
        "last_sync_at": source.last_sync_at.isoformat() if source.last_sync_at else None,
        "last_full_sync_at": (
            source.last_full_sync_at.isoformat() if source.last_full_sync_at else None
        ),
        "last_sync_summary": source.last_sync_summary,
        # A deletion the scans have not agreed on yet. The documents in it are still
        # indexed and still answer searches, so an operator who is not told about it would
        # have no way to know the index is holding documents the source says are gone.
        "pending_deletion": _pending_deletion_payload(session, source.id),
        "object_count": session.scalar(
            select(func.count())
            .select_from(SourceObject)
            .where(SourceObject.source_id == source.id, SourceObject.deleted_at.is_(None))
        )
        or 0,
        **_source_pipeline_counts(session, source.id),
    }


def _source_pipeline_counts(session: Session, source_id: str) -> dict:
    """How much of this connection is searchable, and whether the pipeline owes it work.

    The index stage is the pipeline's finish line, so its per-source state answers the
    two questions the connections page keeps mis-answering from estate-wide totals:
    how many of *this* connection's documents are searchable, and whether the shared
    insertion run is doing anything for *this* connection. Quarantined and
    disabled-by-configuration rows are terminal, not pending — counting them as owed
    work would pin an "indexing" spinner on a connection nothing will ever process.
    """
    index_rows = (
        select(func.count())
        .select_from(ProcessingState)
        .join(SourceObject, SourceObject.id == ProcessingState.source_object_id)
        .where(
            SourceObject.source_id == source_id,
            SourceObject.deleted_at.is_(None),
            ProcessingState.stage == PipelineStage.INDEX.value,
        )
    )
    indexed = session.scalar(
        index_rows.where(ProcessingState.status == ProcessingStatus.DONE.value)
    )
    pending = session.scalar(
        index_rows.where(
            or_(
                ProcessingState.status.in_(
                    [
                        ProcessingStatus.PENDING.value,
                        ProcessingStatus.RUNNING.value,
                        ProcessingStatus.FAILED.value,
                    ]
                ),
                and_(
                    ProcessingState.status == ProcessingStatus.SKIPPED.value,
                    ProcessingState.last_error["reason"].as_string()
                    == WAITING_FOR_PREVIOUS_STAGE,
                ),
            )
        )
    )
    return {"indexed_count": indexed or 0, "pending_pipeline_count": pending or 0}


def _pending_deletion_payload(session: Session, source_id: str) -> dict | None:
    from knowledge_index.sync import deletions

    state = deletions.pending(session, source_id)
    return state.payload() if state is not None else None


def _run_payload(row: PipelineRunRecord) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "source_id": row.source_id,
        "provider": row.provider,
        "provider_run_id": row.provider_run_id,
        "workflow": row.workflow,
        "status": row.status,
        "progress": row.progress,
        "current_step": row.current_step,
        # object_ids is the batch's working set, not a counter: it holds every document
        # UUID in the run. Shipping it to each admin browser on every poll costs hundreds
        # of KB per run, and nothing in the UI can do anything with the list.
        "counters": {key: value for key, value in (row.counters or {}).items() if key != "object_ids"},
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "error": row.last_error,
    }


def _external_client_payload(row: ExternalClient) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
        "principal": row.principal,
        "secret_ref": row.secret_ref,
        "allowed_project_ids": row.allowed_project_ids,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


def _refresh_project_chunk_acl(session: Session, project_id: str) -> None:
    document_ids = session.scalars(
        select(Document.id).where(Document.project_id == project_id)
    ).all()
    for document_id in document_ids:
        _refresh_document_chunk_acl(session, document_id)


def _refresh_document_chunk_acl(session: Session, document_id: str) -> None:
    document = session.get(Document, document_id)
    if document is None:
        return
    grants: list[ProjectGrant | DocumentGrant] = []
    if document.project_id:
        grants.extend(
            session.scalars(
                select(ProjectGrant).where(ProjectGrant.project_id == document.project_id)
            ).all()
        )
    grants.extend(
        session.scalars(select(DocumentGrant).where(DocumentGrant.document_id == document_id)).all()
    )
    allowed = {row.principal for row in grants if row.effect == "allow"}
    denied = {row.principal for row in grants if row.effect == "deny"}
    chunks = session.scalars(select(Chunk).where(Chunk.document_id == document_id)).all()
    for chunk in chunks:
        chunk.allowed_principals = sorted(set(chunk.allowed_principals or []) | allowed)
        chunk.denied_principals = sorted(set(chunk.denied_principals or []) | denied)
        chunk.access_version += 1


def _connector_catalog(config: AppConfig) -> list[dict]:
    # Filesystem-shaped sources added directly from the admin UI; every other
    # connector comes from the connector registry.
    native = [
        {
            "provider": "native",
            "provider_ui_url": None,
            "id": "local_fs",
            "name": "Files from this computer",
            "category": "Filesystem",
            "acl_sync": False,
            "incremental": "watch + rescan",
            "auth": ["none"],
            "continuous": True,
            "extensible": True,
            "native": True,
            "recommended": True,
            "connectable": True,
        },
        {
            "provider": "native",
            "provider_ui_url": None,
            "id": "plugin_drop",
            "name": "Plugin drop directory",
            "category": "Filesystem",
            "acl_sync": True,
            "incremental": "watch + rescan",
            "auth": ["none"],
            "continuous": True,
            "extensible": True,
            "native": True,
            # This is an FDE interchange contract, not a second way for an operator to
            # choose a folder. Keep it addressable through the API and documented for
            # custom DMS integrations without presenting it in the normal catalog.
            "internal": True,
        },
    ]
    return native


# The vocabulary of the browsable tree itself: `get_browse_children` yields sites,
# drives, libraries and folders, and paths are how a schema addresses them in prose.
_TREE_NODE_WORDS = (
    r"\b(folder|folders|subfolder|subfolders|directory|directories|path|paths"
    r"|site|sites|drive|drives|library|libraries|subtree|subtrees|root)\b"
)


def _picker_answers(spec, name: str, field, widget_type: str) -> bool:
    """Whether the folder picker already answers what this config field asks.

    A scoping-capable connector settles "which parts of this source" against the
    provider's own tree, after authorization (docs/src/content/docs/connectors/index.md). Before that
    the appliance has never seen the drive, so a text field asking the same question can
    only be guessed at — the operator is typing folder paths at a system that could show
    them the folders. Those fields are marked so the connect form can defer them; the
    connector still receives its schema default, so nothing about its behaviour changes.

    Identified from what the field *is* rather than from a list of names, which would go
    stale the moment a connector gains a setting: the name, the title and the opening
    sentence of the description state a field's subject (the rest is caveats and
    cross-references), so a field whose subject is a node of the tree is the picker's
    question asked in text. Three guards keep it honest. Only connectors that actually
    have a picker are considered, so Outlook's `included_folders` — mail folders, no
    browsable tree — is untouched. A boolean is never deferred: a checkbox such as
    "include personal sites" is a policy answerable without seeing anything. Neither is
    a required field, which must always have a visible control.
    """
    import re

    if not spec.supports_scoping or widget_type == "boolean" or field.is_required():
        return False
    subject = (field.description or "").strip()
    subject = re.split(r"(?<=[.!?])\s", subject)[0] if subject else ""
    return bool(re.search(_TREE_NODE_WORDS, f"{name} {field.title or ''} {subject}".lower()))


def _registration_guide(spec) -> dict | None:
    """What the firm's IT admin has to do in their own console before this can connect.

    Every deployment registers its own OAuth app, so setup fails in the provider's
    console long before it fails here — a missing admin consent, the Secret ID copied
    instead of the secret Value, a personal account at a work-account endpoint. The
    prose for each console lives in ``providers.yaml`` next to the settings it describes.

    The scope list is *generated* from the provider's own ``scope``, never written into
    the guidance: the panel therefore states exactly what the appliance will ask the
    provider for, and gaining or narrowing a scope updates the instructions with it.
    """
    if not spec.oauth_provider:
        return None
    from knowledge_index.connectors.runtime import oauth as oauth_runtime
    from knowledge_index.connectors.runtime.errors import SourceAuthError

    try:
        provider = oauth_runtime.get_provider(spec.oauth_provider)
    except SourceAuthError:
        return None
    return {
        **_provider_registration_docs().get(spec.oauth_provider, {}),
        "provider": spec.oauth_provider,
        "scopes": (provider.scope or "").split(),
        "oauth_type": provider.oauth_type,
    }


@lru_cache(maxsize=1)
def _provider_registration_docs() -> dict[str, dict]:
    """The `registration:` blocks of providers.yaml, keyed by provider.

    Read from the YAML rather than from ``OAuthProvider``: that dataclass carries only
    what the handshake needs, and documentation has no business widening it.
    """
    import yaml

    from knowledge_index.connectors.runtime import oauth as oauth_runtime

    raw = yaml.safe_load(oauth_runtime.PROVIDERS_PATH.read_text(encoding="utf-8")) or {}
    return {
        name: dict((values or {}).get("registration") or {})
        for name, values in (raw.get("providers") or {}).items()
    }


def _config_fields(spec) -> list[dict]:
    """Render a connector's config form straight off its schema.

    The form follows the connector's own Pydantic model, so a connector that adds a
    setting gets a UI field without an admin-UI change. The annotation and the default
    travel with each field because the label alone is not enough to build a control: a
    bool rendered as a text box lets an operator type prose into it and submits the
    string "true", and a list rendered as one line makes the separator a guess. Sending
    the model's own default also means an untouched form submits what the connector
    would have done anyway rather than an empty value that overrides it.
    """
    import typing

    from pydantic_core import PydanticUndefined

    config_class = getattr(spec.load(), "config_class", None)
    if config_class is None:
        return []

    def widget(annotation) -> str:
        origin = typing.get_origin(annotation)
        if origin in (list, set, tuple):
            return "list"
        if origin is not None:
            # Optional[str] is a str the operator may leave blank, which is what an
            # empty text box already means, so it collapses to a plain text control.
            inner = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
            return widget(inner[0]) if len(inner) == 1 else "text"
        if annotation is bool:
            return "boolean"
        if annotation in (int, float):
            return "integer"
        return "text"

    fields = []
    for name, field in config_class.model_fields.items():
        default = field.get_default(call_default_factory=True)
        widget_type = widget(field.annotation)
        entry = {
            "name": name,
            "title": field.title or name.replace("_", " ").capitalize(),
            "description": field.description or "",
            "required": field.is_required(),
            "type": widget_type,
            "default": None if default is PydanticUndefined else default,
        }
        if _picker_answers(spec, name, field, widget_type):
            entry["superseded_by"] = "folder_picker"
        fields.append(entry)
    return fields


# Matches the pending-authorization TTL: both are "how long a half-finished setup is
# allowed to sit there before the appliance decides nobody is coming back".
UNCLAIMED_IMPORT_TTL_SECONDS = 30 * 60

# The admin UI polls status and runs every few seconds; the sweep itself is one indexed
# query when nothing is stranded, but it may talk to the orchestrator when something is,
# and that must not happen once per poll per open browser tab.
RUN_SWEEP_MIN_INTERVAL_SECONDS = 60.0
_last_run_sweep = 0.0
_run_sweep_lock = threading.Lock()


def _sweep_runs_if_due(
    session_factory: sessionmaker[Session], config: AppConfig, *, force: bool = False
) -> None:
    """Resolve stranded runs before reporting or starting work, at most once a minute.

    Never raises: a sweeper that cannot reach the orchestrator must not take the
    dashboard down with it. Its failure is logged, which is the evidence an operator
    needs — the runs it did not resolve stay visible as running either way.
    """
    global _last_run_sweep
    now = time.monotonic()
    with _run_sweep_lock:
        if not force and now - _last_run_sweep < RUN_SWEEP_MIN_INTERVAL_SECONDS:
            return
        _last_run_sweep = now
    try:
        from knowledge_index.orchestration.sweeper import sweep_stranded_runs

        sweep_stranded_runs(session_factory, config)
    except Exception:
        _log.exception("stranded-run sweep failed; runs may still be reported as in flight")


def _sweep_unclaimed_imports(session: Session, import_parent: Path) -> int:
    """Delete browser-import directories no source ever claimed. Returns how many went.

    Referenced by a source, or newer than the TTL, means keep — a directory younger than
    the TTL may still be the one the caller is about to submit.
    """
    if not import_parent.is_dir():
        return 0
    claimed = {
        str((source.config or {}).get("root"))
        for source in session.scalars(select(Source))
        if (source.config or {}).get("root")
    }
    cutoff = time.time() - UNCLAIMED_IMPORT_TTL_SECONDS
    removed = 0
    for child in import_parent.iterdir():
        if not child.is_dir() or child.is_symlink() or str(child) in claimed:
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    if removed:
        _log.info("reclaimed %d unclaimed browser-import folder(s) from %s", removed, import_parent)
    return removed


def _managed_import_root(session: Session, source: Source, config: AppConfig) -> Path | None:
    """The browser-import directory this source owns outright, if it has one.

    Only directories the appliance itself created under ``browser-sources`` are eligible:
    a ``local_fs`` source usually points at a firm's own mount, and disconnecting it must
    never delete the firm's originals. Two sources can be aimed at one imported folder,
    so a folder another source still claims is left alone.
    """
    root = (source.config or {}).get("root")
    if not root:
        return None
    import_parent = config.artifact_dir.expanduser().resolve().parent / "browser-sources"
    try:
        resolved = Path(str(root)).expanduser().resolve()
    except OSError:
        return None
    if resolved == import_parent or not resolved.is_relative_to(import_parent):
        return None
    for other in session.scalars(select(Source).where(Source.id != source.id)):
        if str((other.config or {}).get("root") or "") == str(root):
            return None
    return resolved


def _reclaim_source_storage(
    session_factory: sessionmaker[Session],
    config: AppConfig,
    *,
    source_id: str,
    candidate_hashes: set[str],
    managed_root: Path | None,
) -> ReclaimReport:
    """Give back the disk a deleted source was using, and only that disk.

    Three kinds of bytes belong to one source. Its connector staging tree is keyed by the
    source id and is nobody else's. A browser-imported folder was created by the
    appliance for this connection alone. The blobs are the hard case: content addressing
    means one file can be the same contract filed in three matters, so a blob only goes
    when its last referent has. A referent is a live ``source_objects`` row or a document
    version that some other source still vouches for.
    """
    from knowledge_index.connectors.registry import staging_root_for_source

    report = artifacts.remove_tree(staging_root_for_source(source_id))
    if managed_root is not None:
        report.absorb(artifacts.remove_tree(managed_root))

    if candidate_hashes:
        ordered = sorted(candidate_hashes)
        with session_factory() as session:
            still_referenced: set[str] = set()
            for batch in (ordered[at : at + 500] for at in range(0, len(ordered), 500)):
                still_referenced.update(
                    session.scalars(
                        select(SourceObject.content_hash).where(
                            SourceObject.content_hash.in_(batch)
                        )
                    )
                )
                still_referenced.update(
                    session.scalars(
                        select(DocumentVersion.content_hash)
                        .join(
                            DocumentVersionSource,
                            DocumentVersionSource.version_id == DocumentVersion.id,
                        )
                        .where(DocumentVersion.content_hash.in_(batch))
                    )
                )
            orphaned = [value for value in ordered if value not in still_referenced]
            report.blobs_retained_shared = len(ordered) - len(orphaned)

            store = LocalArtifactStore(config.artifact_dir)
            report.absorb(store.reclaim_blobs(orphaned))
            _forget_reclaimed_blobs(session, orphaned)
            session.commit()

    if report.failures:
        # Loud, and once per failure: this is the appliance telling an operator that it
        # reported a deletion it did not fully perform.
        for failure in report.failures:
            _log.error("source %s: could not reclaim %s", source_id, failure)
    _log.info(
        "source %s deleted: reclaimed %d file(s), %d byte(s); %d shared blob(s) kept",
        source_id,
        report.files_removed,
        report.bytes_reclaimed,
        report.blobs_retained_shared,
    )
    return report


def _forget_reclaimed_blobs(session: Session, content_hashes: list[str]) -> None:
    """Drop the rows that described bytes that are now gone.

    Derived artifacts are pure caches keyed on the content hash, so once the content has
    no referent they describe nothing and are removed with it — they also hold the
    document's extracted text, which is the same confidential material. A ``blobs`` row
    survives only where an orphaned document version still points at it; that row keeps
    ``cached_path`` cleared so nothing reads a path that no longer exists.
    """
    for batch in (content_hashes[at : at + 500] for at in range(0, len(content_hashes), 500)):
        if not batch:
            continue
        session.execute(delete(Artifact).where(Artifact.content_hash.in_(batch)))
        held = set(
            session.scalars(
                select(DocumentVersion.content_hash).where(
                    DocumentVersion.content_hash.in_(batch)
                )
            )
        )
        removable = [value for value in batch if value not in held]
        if removable:
            session.execute(delete(Blob).where(Blob.content_hash.in_(removable)))
        if held:
            session.execute(
                Blob.__table__.update()
                .where(Blob.content_hash.in_(sorted(held)))
                .values(cached_path=None)
            )


def _handshake_expired(credentials: dict) -> bool:
    """Whether a re-authorization's single-use state has passed its deadline.

    Fails closed: a credential blob carrying a state but no readable deadline predates
    this check or was tampered with, and either way is not something to redeem.
    """
    raw = credentials.get("oauth_state_expires_at")
    try:
        deadline = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return deadline <= datetime.now(UTC)


def _without_handshake(credentials: dict) -> dict:
    """The same credentials with the single-use handshake fields removed."""
    return {
        key: value
        for key, value in credentials.items()
        if key not in {"oauth_state", "oauth_code_verifier", "oauth_state_expires_at"}
    }


def _reject_unconfirmed_broad_grant(spec, payload: "SourceCreate") -> None:
    """Refuse to publish one person's mailbox to a group without explicit confirmation.

    ``default_acl`` exists so an operator can make an otherwise-invisible source
    retrievable. On a mailbox or a personal drive that same gesture exposes a single
    lawyer's entire correspondence to whoever is named — usually not what the operator
    thinks they are doing, and not something to discover after the fact.
    """
    if not getattr(spec, "private_corpus", False) or not payload.default_acl:
        return
    broad = [
        grant.get("principal")
        for grant in payload.default_acl
        if str(grant.get("principal", "")).partition(":")[0] in {"group", "role"}
    ]
    if broad and not payload.confirm_broad_grant:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{spec.label} holds one person's correspondence. Granting it to "
                f"{', '.join(str(item) for item in broad)} would expose that mailbox to "
                "everyone in that group. Re-submit with confirm_broad_grant=true if that "
                "is intended."
            ),
        )


def _source_mirrors_acls(source) -> bool | None:
    """Whether this source's permissions are mirrored, or None for a local source."""
    from knowledge_index.connectors.registry import BY_NAME

    spec = BY_NAME.get(source.kind)
    return spec.mirrors_acls if spec else None


def _source_scope(source) -> dict:
    """Scope summary attached to every source payload."""
    from knowledge_index.connectors import scoping

    return scoping.describe((source.config or {}).get("connector"))
