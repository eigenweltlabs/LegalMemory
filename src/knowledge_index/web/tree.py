"""Directory projection over the authorized source-object estate.

The document ledger answers "what is indexed". A file browser asks a different
question — "what is in this folder" — and the ledger cannot answer it without
shipping every row it has. At fifty thousand documents that is a twenty-megabyte
response, and a client that has to reconstruct the whole tree before it can draw
the three levels somebody actually expanded.

So the folder structure is computed where the paths already live. One aggregate
on the next path segment under a prefix returns the subfolders; one paginated
scan returns the files directly in it. Both run behind the same permission
predicate as every other read, applied at both the version and the source-object
scope, because a folder listing that names a file the caller may not open has
already leaked the thing the compiler exists to protect: the path.

Paths are stored verbatim, which means a connector's own separator and a
connector's own idea of whether a path is absolute. Both are normalized here
rather than in the query callers write, so `/matters/A` and `matters\\A` are one
folder and not two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from knowledge_index.db.models import (
    Document,
    DocumentVersion,
    DocumentVersionSource,
    Matter,
    Source,
    SourceObject,
    SourceObjectGrant,
)
from knowledge_index.permissions import AccessService

# A folder listing is a navigation aid, not a result set: a directory with more
# subfolders than this is a directory nobody scrolls, and the cost of counting
# them all is paid on every expand. Files are paginated instead, so this bounds
# only the aggregate.
MAX_FOLDERS = 2_000
DEFAULT_FILE_PAGE = 200
MAX_FILE_PAGE = 1_000


@dataclass(frozen=True)
class TreePage:
    source_id: str
    path: str
    folders: list[dict]
    files: list[dict]
    total_files: int
    offset: int
    limit: int

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "path": self.path,
            "folders": self.folders,
            "files": self.files,
            "pagination": {
                "total": self.total_files,
                "offset": self.offset,
                "limit": self.limit,
                "returned": len(self.files),
                "has_more": self.offset + len(self.files) < self.total_files,
            },
        }


def _normalized_path() -> ColumnElement[str]:
    """`SourceObject.path` as one comparable shape: forward slashes, leading slash.

    Connectors disagree about both. A Windows share reports ``Matters\\A\\brief.docx``
    and Google Drive reports ``EWL/brief.docx``; neither is wrong, but a prefix
    comparison across them only works if the disagreement is settled before the
    comparison, and settled identically for the stored value and the prefix the
    caller asked about (see `normalize_path`).
    """

    forward = func.replace(SourceObject.path, "\\", "/")
    return case((forward.like("/%"), forward), else_=literal("/") + forward)


def normalize_path(path: str | None) -> str:
    """The `_normalized_path` rule applied to a caller-supplied prefix.

    Returns "" for the source root, otherwise an absolute path with no trailing
    slash — the form every query below builds its ``LIKE`` pattern from.
    """

    if not path:
        return ""
    cleaned = path.replace("\\", "/").rstrip("/")
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith("/") else "/" + cleaned


def _like_prefix(prefix: str) -> str:
    """``prefix/%`` with the caller's wildcards defused.

    A path may legitimately contain ``%`` or ``_``; unescaped, a folder named
    ``100%_final`` would list the contents of every sibling that happens to match.
    """

    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"


def _position(session: Session, needle: str, haystack: ColumnElement[str]) -> ColumnElement[int]:
    """1-based index of ``needle`` in ``haystack``, 0 when absent.

    Postgres spells this ``strpos`` and SQLite spells it ``instr`` with the
    arguments the other way round. The appliance runs on the first and its tests
    on the second, so both are spelled out rather than pinning the feature to
    whichever one happened to be in front of the author.
    """

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        return func.strpos(haystack, needle)
    return func.instr(haystack, needle)


class DocumentTreeService:
    """Folder listings over the source objects a caller is allowed to see."""

    def __init__(self, session: Session, principals: set[str]) -> None:
        self.session = session
        self.principals = principals
        self.access = AccessService(session)

    # ------------------------------------------------------------------ access

    def _source_object_predicate(self) -> ColumnElement[bool]:
        """The source-object half of the compiler, as SQL.

        `RetrievalService._authorized_sources` decides this per source object in
        Python, which is right for one document and wrong for a folder: the same
        decision for every object under a prefix has to be a predicate the
        database can apply while it aggregates. The rules are the ones stated
        there — deny wins; an explicit mirrored allow admits; and only the local
        filesystem adapter, which has no ACL of its own to mirror, may fall back
        to the project boundary. Every other connector fails closed on a gap.
        """

        if AccessService.is_admin(self.principals):
            return literal(True)
        normalized = sorted(self.access.resolve_principals(self.principals))
        if not normalized:
            return literal(False)
        has_any_grant = exists(
            select(SourceObjectGrant.id).where(
                SourceObjectGrant.source_object_id == SourceObject.id
            )
        )
        allow = exists(
            select(SourceObjectGrant.id).where(
                SourceObjectGrant.source_object_id == SourceObject.id,
                SourceObjectGrant.principal.in_(normalized),
                SourceObjectGrant.effect == "allow",
            )
        )
        deny = exists(
            select(SourceObjectGrant.id).where(
                SourceObjectGrant.source_object_id == SourceObject.id,
                SourceObjectGrant.principal.in_(normalized),
                SourceObjectGrant.effect == "deny",
            )
        )
        local_delegation = and_(~has_any_grant, Source.kind == "local_fs")
        return and_(~deny, or_(allow, local_delegation))

    def _visible_objects(self):
        """Source objects joined to the document they carry, ACL-filtered.

        The join is the authorization: an object with no live document version
        the caller may read is not a file in this estate, it is a file on
        somebody's disk that this appliance happens to have a row about.
        """

        return (
            select(SourceObject.id.label("source_object_id"))
            .select_from(SourceObject)
            .join(Source, Source.id == SourceObject.source_id)
            .join(
                DocumentVersionSource,
                DocumentVersionSource.source_object_id == SourceObject.id,
            )
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentVersionSource.version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                SourceObject.deleted_at.is_(None),
                self.access.version_predicate(self.principals),
                self._source_object_predicate(),
            )
        )

    def _entries(self, *, source_id: str, prefix: str):
        """Everything under ``prefix``, already split into its next path segment.

        A subquery rather than a bare expression list, and not for tidiness: the
        segment is built from bound parameters, and Postgres matches GROUP BY
        against SELECT syntactically on the parse tree, where two occurrences of
        the same expression carrying different parameter placeholders are two
        different expressions. Computing it once in an inner select and grouping
        on the resulting column sidesteps that entirely, and keeps the outer
        queries legible besides.
        """

        full = _normalized_path()
        # +2, not +1: substr is 1-based and the separator after the prefix is
        # consumed too, so `relative` is what follows `prefix/`.
        relative = func.substr(full, len(prefix) + 2)
        separator = _position(self.session, "/", relative)
        segment = case(
            (separator > 0, func.substr(relative, 1, separator - 1)),
            else_=relative,
        )
        return (
            self._visible_objects()
            .with_only_columns(
                SourceObject.id.label("source_object_id"),
                segment.label("segment"),
                separator.label("separator"),
            )
            .where(
                SourceObject.source_id == source_id,
                full.like(_like_prefix(prefix), escape="\\"),
            )
            # One source object can carry several document versions; without this
            # a file with three versions appears three times in its own folder.
            .distinct()
            .subquery()
        )

    # ------------------------------------------------------------------- roots

    def roots(self) -> list[dict]:
        """One node per connector, with the file count the caller can actually see.

        The count is the caller's, not the source's. Two lawyers opening the same
        appliance see the same connectors and different numbers under them, which
        is the ethical wall being visible rather than merely enforced.
        """

        counts = dict(
            self.session.execute(
                self._visible_objects()
                .with_only_columns(
                    SourceObject.source_id,
                    func.count(func.distinct(SourceObject.id)),
                )
                .group_by(SourceObject.source_id)
            ).all()
        )
        rows = self.session.scalars(
            select(Source).where(Source.id.in_(counts.keys())).order_by(Source.display_name)
        ).all()
        return [
            {
                "source_id": row.id,
                "display_name": row.display_name,
                "kind": row.kind,
                "project_id": row.project_id,
                "status": row.status,
                "files": int(counts.get(row.id, 0)),
            }
            for row in rows
        ]

    # ---------------------------------------------------------------- children

    def children(
        self,
        *,
        source_id: str,
        path: str | None = None,
        offset: int = 0,
        limit: int = DEFAULT_FILE_PAGE,
    ) -> TreePage:
        """Subfolders and one page of files directly under ``path``.

        Folders come back whole because a client cannot render a tree it has to
        paginate sideways; files come back a page at a time because a matter
        folder with ten thousand exhibits in it is a normal thing for a firm to
        have and an abnormal thing to send over a wire.
        """

        prefix = normalize_path(path)
        limit = max(1, min(limit, MAX_FILE_PAGE))
        offset = max(0, offset)
        entries = self._entries(source_id=source_id, prefix=prefix)

        folder_rows = self.session.execute(
            select(
                entries.c.segment,
                func.count(func.distinct(entries.c.source_object_id)),
            )
            .where(entries.c.separator > 0)
            .group_by(entries.c.segment)
            .order_by(entries.c.segment)
            .limit(MAX_FOLDERS)
        ).all()
        folders = [
            {
                "name": name,
                "path": f"{prefix}/{name}",
                "files": int(count),
            }
            for name, count in folder_rows
            if name
        ]

        total_files = int(
            self.session.scalar(
                select(func.count(func.distinct(entries.c.source_object_id))).where(
                    entries.c.separator == 0
                )
            )
            or 0
        )
        page_ids = self.session.execute(
            select(entries.c.source_object_id)
            .where(entries.c.separator == 0)
            .order_by(entries.c.segment, entries.c.source_object_id)
            .offset(offset)
            .limit(limit)
        ).all()

        return TreePage(
            source_id=source_id,
            path=prefix,
            folders=folders,
            files=self._file_payloads([row.source_object_id for row in page_ids]),
            total_files=total_files,
            offset=offset,
            limit=limit,
        )

    # ------------------------------------------------------------------ locate

    def locate(self, document_id: str) -> dict | None:
        """Where a document sits in the tree, and on which page of its folder.

        This is what turns a document id in a model's answer into a revealed node
        on the left. Returning the ancestor chain means the client expands exactly
        the folders on the path instead of walking the tree looking for it, and
        returning the file's ordinal within its own folder means it can jump
        straight to the page holding it rather than scrolling a ten-thousand-file
        directory until the highlight appears.
        """

        # Same reason `_entries` is a subquery: ordering by an expression built
        # from bound parameters is not, to Postgres, ordering by the column of
        # the same shape in the select list.
        candidates = (
            self._visible_objects()
            .with_only_columns(
                SourceObject.id.label("source_object_id"),
                SourceObject.source_id.label("source_id"),
                _normalized_path().label("full_path"),
            )
            .where(Document.id == document_id)
            .distinct()
            .subquery()
        )
        row = self.session.execute(
            select(candidates)
            .order_by(candidates.c.full_path, candidates.c.source_object_id)
            .limit(1)
        ).first()
        if row is None:
            return None

        full_path: str = row.full_path
        folder, _, name = full_path.rpartition("/")
        prefix = normalize_path(folder)

        siblings = self._entries(source_id=row.source_id, prefix=prefix)
        # The file's position under the same (segment, id) ordering `children`
        # pages by, so the client's page arithmetic and the server's agree.
        index = int(
            self.session.scalar(
                select(func.count(func.distinct(siblings.c.source_object_id))).where(
                    siblings.c.separator == 0,
                    or_(
                        siblings.c.segment < name,
                        and_(
                            siblings.c.segment == name,
                            siblings.c.source_object_id < row.source_object_id,
                        ),
                    ),
                )
            )
            or 0
        )

        ancestors: list[str] = []
        walked = ""
        for part in [item for item in prefix.split("/") if item]:
            walked = f"{walked}/{part}"
            ancestors.append(walked)

        payloads = self._file_payloads([row.source_object_id])
        return {
            "source_id": row.source_id,
            "path": prefix,
            "ancestors": ancestors,
            "index": index,
            "file": payloads[0] if payloads else None,
        }

    # ------------------------------------------------------------------ search

    def search(self, query: str, *, limit: int = 50) -> list[dict]:
        """Filename search across the estate, so the tree has a way in besides walking.

        Matches the file name rather than the whole path: a lawyer typing
        "whitford" is naming a document, and matching the path as well would rank
        every file in a folder that happens to be called Whitford above the
        declaration they meant.
        """

        needle = query.strip()
        if not needle:
            return []
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.session.execute(
            self._visible_objects()
            .with_only_columns(SourceObject.id.label("source_object_id"))
            .where(SourceObject.name.ilike(f"%{escaped}%", escape="\\"))
            .distinct()
            .order_by(SourceObject.id)
            .limit(max(1, min(limit, 200)))
        ).all()
        return self._file_payloads([row.source_object_id for row in rows])

    # ----------------------------------------------------------------- payload

    def _file_payloads(self, source_object_ids: list[str]) -> list[dict]:
        """Hydrate one bounded page of source objects into tree nodes.

        Deliberately a second query over an id list rather than more columns on
        the aggregate: the aggregate scans the estate, and every column added to
        it is carried across rows that were never going to be returned.
        """

        if not source_object_ids:
            return []
        objects = {
            row.id: row
            for row in self.session.scalars(
                select(SourceObject).where(SourceObject.id.in_(source_object_ids))
            ).all()
        }
        linkage = self.session.execute(
            select(
                DocumentVersionSource.source_object_id,
                Document.id,
                Document.title,
                Document.doc_type,
                Document.language,
                Document.matter_id,
                Document.project_id,
                DocumentVersion.id,
                DocumentVersion.status,
                DocumentVersion.ordinal,
                DocumentVersion.content_hash,
            )
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentVersionSource.version_id,
            )
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersionSource.source_object_id.in_(source_object_ids))
            .order_by(
                DocumentVersionSource.source_object_id,
                DocumentVersion.ordinal.desc().nullslast(),
                DocumentVersion.created_at.desc(),
            )
        ).all()
        # One file, one node: where an object carries several versions the newest
        # wins the node and the rest stay reachable through the document itself.
        best: dict[str, Any] = {}
        for record in linkage:
            best.setdefault(record[0], record)

        matter_ids = {record[5] for record in best.values() if record[5]}
        matters = {
            row.id: row
            for row in self.session.scalars(
                select(Matter).where(Matter.id.in_(matter_ids))
            ).all()
        }

        payloads: list[dict] = []
        for source_object_id in source_object_ids:
            source_object = objects.get(source_object_id)
            record = best.get(source_object_id)
            if source_object is None or record is None:
                continue
            matter = matters.get(record[5])
            payloads.append(
                {
                    "source_object_id": source_object.id,
                    "source_id": source_object.source_id,
                    "name": source_object.name,
                    "path": normalize_path(source_object.path),
                    "mime_type": source_object.mime_type,
                    "size_bytes": source_object.size_bytes,
                    "mtime": source_object.mtime.isoformat() if source_object.mtime else None,
                    "document_id": record[1],
                    "title": record[2],
                    "doc_type": record[3],
                    "language": record[4],
                    "matter_id": record[5],
                    "project_id": record[6],
                    "version_id": record[7],
                    "version_status": record[8],
                    "version_ordinal": record[9],
                    "content_hash": record[10],
                    "matter": (
                        {
                            "id": matter.id,
                            "title": matter.title,
                            "practice_area": matter.practice_area,
                            "status": matter.status,
                        }
                        if matter
                        else None
                    ),
                }
            )
        return payloads
