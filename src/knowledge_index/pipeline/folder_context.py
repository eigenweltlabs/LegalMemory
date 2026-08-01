"""Folder-structure context for AI relation inference.

The relate stage does not use regex or byte-hash equality to decide what is a
version, a duplicate, or an annex. Instead it hands the model the same thing a
paralegal sees: the folder tree, the neighbouring folders, and the contents of
the documents in a matter. The model reasons about identity and relationships
from that context. This module assembles that context.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_index.db.models import (
    Artifact,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    SourceObject,
)


def _parent(path: str) -> str:
    """Directory portion of a POSIX-ish source path (no regex, no os.path)."""
    cut = path.rstrip("/").rfind("/")
    return path[:cut] if cut > 0 else ""


def _basename(path: str) -> str:
    cut = path.rstrip("/").rfind("/")
    return path[cut + 1 :] if cut >= 0 else path


def _depth(folder: str) -> int:
    return 0 if not folder else folder.count("/") + 1


def folder_ls(
    session: Session,
    source_id: str,
    locus_path: str,
    *,
    up: int = 2,
    down: int = 2,
    per_folder_limit: int | None = 80,
    max_folders: int | None = 200,
) -> str:
    """A text ``ls`` of the folder neighbourhood around one document.

    Lists the document's own folder in full, plus ``up`` ancestor levels and ``down``
    descendant levels, each with their direct files and subfolders. This is the same
    locus a paralegal sees when deciding which matter a file belongs to — it lets the
    classification and relation agents reason about location-based grouping without
    loading the whole estate. Paths only; cheap even on large estates.
    """
    paths = session.scalars(
        select(SourceObject.path).where(
            SourceObject.source_id == source_id, SourceObject.deleted_at.is_(None)
        )
    ).all()
    files_by_folder: dict[str, list[str]] = {}
    all_folders: set[str] = {""}
    for path in paths:
        folder = _parent(path)
        files_by_folder.setdefault(folder, []).append(_basename(path))
        cursor = folder
        while cursor:
            all_folders.add(cursor)
            cursor = _parent(cursor)
    child_folders: dict[str, set[str]] = {}
    for folder in all_folders:
        if folder:
            child_folders.setdefault(_parent(folder), set()).add(folder)

    locus = _parent(locus_path)
    locus_depth = _depth(locus)

    # ancestor spine: the locus folder plus `up` levels above it.
    spine: set[str] = {locus}
    cursor = locus
    for _ in range(up):
        parent = _parent(cursor)
        if parent == cursor:
            break
        spine.add(parent)
        if not parent:
            break
        cursor = parent

    def is_locus_descendant(folder: str) -> bool:
        if locus == "":
            return _depth(folder) <= down
        if folder == locus or folder.startswith(locus + "/"):
            return _depth(folder) - locus_depth <= down
        return False

    # "fully" folders get their files listed; "name_only" folders (siblings/aunts of the
    # spine) are shown as a folder name + a size hint, so other matters are visible without
    # exploding their contents into the prompt.
    fully = {folder for folder in all_folders if folder in spine or is_locus_descendant(folder)}
    name_only: set[str] = set()
    for ancestor in spine:
        for child in child_folders.get(ancestor, ()):
            if child not in fully:
                name_only.add(child)

    display = sorted(fully | name_only)
    if max_folders is not None:
        display = display[:max_folders]
    top_depth = min((_depth(folder) for folder in display), default=0)
    lines: list[str] = []
    for folder in display:
        indent = "  " * max(0, _depth(folder) - top_depth)
        name = _basename(folder) if folder else "/"
        if folder in fully:
            marker = "   <-- this document's folder" if folder == locus else ""
            lines.append(f"{indent}{name}/{marker}")
            files = sorted(files_by_folder.get(folder, []))
            visible_files = files if per_folder_limit is None else files[:per_folder_limit]
            for filename in visible_files:
                lines.append(f"{indent}  {filename}")
            if per_folder_limit is not None and len(files) > per_folder_limit:
                lines.append(f"{indent}  ... (+{len(files) - per_folder_limit} more files)")
        else:
            file_count = len(files_by_folder.get(folder, []))
            sub_count = len(child_folders.get(folder, ()))
            hints = []
            if sub_count:
                hints.append(f"{sub_count} subfolder(s)")
            if file_count:
                hints.append(f"{file_count} file(s)")
            suffix = f"   ({', '.join(hints)})" if hints else ""
            lines.append(f"{indent}{name}/{suffix}")
    return "\n".join(lines)


def list_one_folder(
    session: Session,
    source_id: str,
    folder: str,
    *,
    per_folder_limit: int = 200,
) -> str:
    """Direct contents of one exact folder: its files plus its subfolder names.

    The relation agent's neighbourhood shows sibling folders name-only; this is
    the tool that looks inside them (and, transitively, anywhere: every listing
    reveals more listable names). '' or '/' lists the source root. Bounded output;
    an unknown folder is an honest error, never an empty listing.
    """
    requested = (folder or "").strip().strip("/")
    paths = session.scalars(
        select(SourceObject.path).where(
            SourceObject.source_id == source_id, SourceObject.deleted_at.is_(None)
        )
    ).all()
    all_folders: set[str] = {""}
    files_by_folder: dict[str, list[str]] = {}
    for path in paths:
        parent = _parent(path)
        files_by_folder.setdefault(parent, []).append(_basename(path))
        cursor = parent
        while cursor:
            all_folders.add(cursor)
            cursor = _parent(cursor)
    if requested not in all_folders:
        return f"no such folder: {folder!r}"
    subfolders = sorted(
        candidate for candidate in all_folders if candidate and _parent(candidate) == requested
    )
    lines = [f"{requested or '/'}/"]
    for subfolder in subfolders[:per_folder_limit]:
        file_count = len(files_by_folder.get(subfolder, []))
        suffix = f"   ({file_count} file(s))" if file_count else ""
        lines.append(f"  {_basename(subfolder)}/{suffix}")
    if len(subfolders) > per_folder_limit:
        lines.append(f"  ... (+{len(subfolders) - per_folder_limit} more subfolders)")
    files = sorted(files_by_folder.get(requested, []))
    for filename in files[:per_folder_limit]:
        lines.append(f"  {filename}")
    if len(files) > per_folder_limit:
        lines.append(f"  ... (+{len(files) - per_folder_limit} more files)")
    return "\n".join(lines)


def revisions_digest(
    revisions: list[dict] | None,
    *,
    max_entries: int,
    max_chars: int,
) -> dict | None:
    """A bounded fingerprint of a document's tracked changes for the relate prompt.

    The converted text is the ACCEPTED view — deleted text exists nowhere else, so
    without this digest a redline and a clean draft are indistinguishable. Longest
    revisions first: long insertions/deletions are the most distinctive anchors for
    matching a markup to its base, and Word's run-fragmentation noise sorts itself
    to the bottom. The total count is always reported so the model knows what the
    sample omits.
    """
    if not revisions:
        return None
    ranked = sorted(revisions, key=lambda item: len(str(item.get("text") or "")), reverse=True)
    changes: list[dict] = []
    spent = 0
    for revision in ranked:
        if len(changes) >= max_entries or spent >= max_chars:
            break
        text = str(revision.get("text") or "")[:200]
        spent += len(text)
        changes.append(
            {
                "kind": revision.get("kind"),
                "text": text,
                "author": revision.get("author"),
                "date": revision.get("date"),
            }
        )
    return {"count": len(revisions), "changes": changes}


@dataclass
class MemberDoc:
    source_object_id: str
    path: str
    folder: str
    filename: str
    text: str
    tracked_changes: dict | None
    is_email: bool
    email_subject: str | None
    email_message_id: str | None
    email_in_reply_to: str | None
    content_hash: str | None = None

    def as_prompt(self, *, text_chars: int) -> dict:
        payload = {
            "ref": self.source_object_id,
            "path": self.path,
            "filename": self.filename,
            "text": self.text[:text_chars],
            "tracked_changes": self.tracked_changes,
        }
        if self.is_email:
            payload["email"] = {
                "subject": self.email_subject,
                "message_id": self.email_message_id,
                "in_reply_to": self.email_in_reply_to,
            }
        return payload


def _latest_artifact(session: Session, content_hash: str | None, kind: str) -> Artifact | None:
    if not content_hash:
        return None
    return session.scalar(
        select(Artifact)
        .where(Artifact.content_hash == content_hash, Artifact.kind == kind)
        .order_by(Artifact.created_at.desc())
    )


def build_member_doc(session: Session, source_object: SourceObject) -> MemberDoc:
    """Build relation context for exactly one source object."""
    converted = _latest_artifact(session, source_object.content_hash, "structured_json")
    if converted is None:
        raise ValueError("source object has no structured conversion artifact")
    payload = converted.payload or {}
    metadata = payload.get("metadata") or {}
    revisions = payload.get("revisions") or []
    folder = _parent(source_object.path)
    return MemberDoc(
        source_object_id=source_object.id,
        path=source_object.path,
        folder=folder,
        filename=source_object.name,
        text=str(payload.get("text") or ""),
        tracked_changes=revisions_digest(revisions, max_entries=40, max_chars=4000),
        is_email=source_object.name.casefold().endswith(".eml"),
        email_subject=metadata.get("subject"),
        email_message_id=metadata.get("message_id"),
        email_in_reply_to=metadata.get("in_reply_to"),
        content_hash=source_object.content_hash,
    )


def linked_version(session: Session, source_object_id: str) -> DocumentVersion | None:
    link = session.scalar(
        select(DocumentVersionSource).where(
            DocumentVersionSource.source_object_id == source_object_id
        )
    )
    return session.get(DocumentVersion, link.version_id) if link else None


def linked_document(session: Session, source_object_id: str) -> Document | None:
    version = linked_version(session, source_object_id)
    return session.get(Document, version.document_id) if version else None
