"""Content staging for connectors.

Connectors call two methods — ``download_from_url`` and ``save_bytes`` — and both set
``entity.local_path`` to a file the insertion pipeline then converts.

Three properties of the staging layout are load-bearing:

**Keyed by external id, never by filename.** A firm has ``Vertrag.pdf`` in fifty
matters. Keying staged content on the file name would let one matter's document be
served as another's — a confidentiality failure across a matter boundary, not a mere
overwrite.

**Durable, not temporary.** Enumeration happens in one process and the pipeline fetches
in another, so staged content has to outlive the scan. Otherwise every fetch misses and
falls back to re-crawling the whole source, which is quadratic in document count and
gets the firm's tenant throttled.

**Version-addressed, so unchanged content is not re-downloaded.** The path includes the
source's etag/ctag. A rescan of a million-document estate then costs one metadata pass
plus downloads for genuinely changed files.

Skips raise ``FileSkippedException``, which connectors catch to continue past one
unusable object.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import httpx

from knowledge_index.connectors.entities.flags import entity_external_id, entity_version_token
from knowledge_index.connectors.runtime.errors import FileSkippedException

MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024

# Extensions the insertion pipeline can convert. Anything else is skipped before we
# spend bandwidth and disk on it.
SUPPORTED_FILE_EXTENSIONS = frozenset(
    {
        ".pdf", ".doc", ".docx", ".rtf", ".odt", ".txt", ".md", ".markdown",
        ".html", ".htm", ".xml", ".eml", ".msg",
        ".xls", ".xlsx", ".ods", ".csv", ".tsv",
        ".ppt", ".pptx", ".odp",
        ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".json",
    }
)

NO_VERSION = "noversion"


def safe_filename(filename: str) -> str:
    """Strip path structure and hostile characters from a source-supplied name.

    Source names are adversarial in the way that matters: a document literally called
    ``../../config.json`` must not escape the staging directory.
    """
    base = os.path.basename(filename or "").strip() or "download"
    cleaned = "".join(char if (char.isalnum() or char in "._- ") else "_" for char in base)
    cleaned = cleaned.strip(". ") or "download"
    return cleaned[:200]


def credential_allowed_for(url: str, auth_hosts: tuple[str, ...]) -> bool:
    """Whether the firm's token may be sent to ``url``.

    A download URL is not always the provider's own. A Notion page can hold an
    "external" file block whose address the page's editor typed in themselves; Graph and
    Box hand back pre-signed URLs on CDN hosts that want no credential at all. The token
    therefore travels only to hosts the connector names: anyone who can edit a page in
    the connected workspace can otherwise choose where it goes, and it reads everything
    the connector can reach.

    Matching is on the registrable host and its subdomains, never a substring:
    ``notion.so.evil.com`` is not Notion.
    """
    host = (urlparse(str(url)).hostname or "").lower().rstrip(".")
    if not host:
        return False
    for allowed in auth_hosts:
        candidate = allowed.lower().strip().lstrip(".").rstrip(".")
        if candidate and (host == candidate or host.endswith(f".{candidate}")):
            return True
    return False


def _validate_extension(filename: str) -> str | None:
    _, ext = os.path.splitext(filename or "")
    if ext.lower() not in SUPPORTED_FILE_EXTENSIONS:
        return f"unsupported file extension: {ext or '(none)'}"
    return None


def _key(external_id: str) -> str:
    """A filesystem-safe, collision-free directory name for one external id.

    Hashed rather than sanitized: Graph and Drive ids contain characters a sanitizer
    would fold together, and two ids mapping to one directory is exactly the
    cross-matter content mix-up this layout exists to prevent.
    """
    digest = hashlib.blake2b(external_id.encode("utf-8"), digest_size=16).hexdigest()
    return f"{digest[:2]}/{digest}"


class FileService:
    """Stages connector content under one durable directory per source."""

    MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_BYTES

    def __init__(self, base_dir: str | Path, *, run_id: str = "sync") -> None:
        self.base_dir = (Path(base_dir) / safe_filename(run_id)).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # -- paths --------------------------------------------------------------

    def target_for(self, external_id: str, version: str | None, filename: str) -> Path:
        directory = self.base_dir / _key(external_id) / safe_filename(version or NO_VERSION)
        candidate = (directory / safe_filename(filename)).resolve()
        if not candidate.is_relative_to(self.base_dir):
            raise FileSkippedException("staged path escaped the staging directory", filename)
        return candidate

    def staged_path(self, external_id: str, version: str | None, filename: str) -> Path | None:
        """The staged file for this exact version, if it is already on disk."""
        target = self.target_for(external_id, version, filename)
        return target if target.is_file() else None

    # -- writing ------------------------------------------------------------

    async def download_from_url(
        self,
        entity,
        client,
        auth=None,
        logger=None,
        *,
        headers: dict | None = None,
        auth_hosts: tuple[str, ...] = (),
    ):
        """Stream ``entity.url`` to durable storage and set ``entity.local_path``.

        ``auth_hosts`` names the hosts the connector's credential belongs to. It is
        empty by default and the credential is withheld when it does not match, because
        the failure that costs a firm its token is silent and the failure this causes is
        a visible 401.
        """
        external_id = entity_external_id(entity) or ""
        if not external_id:
            raise FileSkippedException("entity has no stable id to stage against", "")
        filename = entity.name or os.path.basename(str(entity.url).split("?")[0])
        skip = _validate_extension(filename)
        if skip:
            raise FileSkippedException(skip, filename)

        version = entity_version_token(entity)
        existing = self.staged_path(external_id, version, filename)
        if existing is not None:
            # Same id, same source version: the bytes cannot have changed. Skipping the
            # request is what makes a rescan proportional to churn, not to corpus size.
            entity.local_path = str(existing)
            if logger:
                logger.debug(f"reusing staged content for {filename}")
            return entity

        request_headers = dict(headers or {})
        may_send_credential = credential_allowed_for(entity.url, auth_hosts)
        if not may_send_credential and "Authorization" in request_headers:
            # Also covers a caller that built the header itself.
            request_headers.pop("Authorization")
        if auth is not None and "Authorization" not in request_headers:
            if may_send_credential:
                token = await _maybe_token(auth)
                if token:
                    request_headers["Authorization"] = f"Bearer {token}"
            elif logger:
                host = urlparse(str(entity.url)).hostname or "(none)"
                logger.debug(
                    f"fetching {filename} from {host} without credentials: it is not one "
                    "of the hosts this connector's token belongs to"
                )

        target = self.target_for(external_id, version, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        written = 0
        try:
            async with client.stream("GET", str(entity.url), headers=request_headers) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise httpx.HTTPStatusError(
                        f"download failed with {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > self.MAX_FILE_SIZE_BYTES:
                    raise FileSkippedException(
                        f"file too large: {int(declared) / 1024 / 1024:.1f}MB "
                        f"(max {self.MAX_FILE_SIZE_BYTES // 1024 // 1024}MB)",
                        filename,
                    )
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        # Enforced during the write, not only from Content-Length: a
                        # source that under-reports its size must not be able to fill
                        # the appliance's disk and take the index down for the firm.
                        if written > self.MAX_FILE_SIZE_BYTES:
                            raise FileSkippedException(
                                "file exceeded "
                                f"{self.MAX_FILE_SIZE_BYTES // 1024 // 1024}MB during download",
                                filename,
                            )
                        handle.write(chunk)
            # Publish atomically: a crashed download must never be picked up as complete
            # content and indexed truncated.
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        entity.local_path = str(target)
        return entity

    async def save_bytes(self, entity, content: bytes, filename_with_extension: str, logger=None):
        """Persist already-materialized bytes (mail bodies, attachments, exports)."""
        external_id = entity_external_id(entity) or ""
        if not external_id:
            raise FileSkippedException("entity has no stable id to stage against", "")
        skip = _validate_extension(filename_with_extension)
        if skip:
            raise FileSkippedException(skip, filename_with_extension)
        if len(content) > self.MAX_FILE_SIZE_BYTES:
            raise FileSkippedException(
                f"content too large: {len(content) / 1024 / 1024:.1f}MB", filename_with_extension
            )
        entity.local_path = str(
            self._write(external_id, entity_version_token(entity), filename_with_extension, content)
        )
        return entity

    def stage_text(self, external_id: str, text: str, *, version: str | None = None) -> Path:
        """Write a text-only entity (a chat message, a page body) to a staged file.

        Connectors like Teams produce no binary content — their entity *is* the text.
        The pipeline works on files, so the text is materialized here rather than adding
        a second content path through the engine.
        """
        return self._write(external_id, version, "content.txt", text.encode("utf-8"))

    def _write(self, external_id: str, version: str | None, filename: str, payload: bytes) -> Path:
        target = self.target_for(external_id, version, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        try:
            partial.write_bytes(payload)
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return target

    def cleanup(self) -> None:
        """Remove this source's staging directory."""
        shutil.rmtree(self.base_dir, ignore_errors=True)


async def _maybe_token(auth) -> str | None:
    getter = getattr(auth, "get_token", None)
    if getter is None:
        return None
    return await getter()
