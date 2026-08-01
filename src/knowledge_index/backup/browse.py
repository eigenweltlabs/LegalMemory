"""Letting an administrator pick a backup folder instead of typing a path.

A path typed into a text box is a path nobody can check until the first backup fails on
it. It is also the wrong question: the box asks where the folder is *inside this
container*, which is not a thing the person configuring backups has any reason to know.

So this lists what the appliance can actually see and write to, and the UI walks it. Two
ideas keep it honest:

* **places, not a filesystem.** Browsing starts from the volumes this appliance has been
  given — the backup mount, the data directory, anything mounted under /mnt or /media —
  with free space on each, because "which disk" is the decision and free space is how it
  is made. Navigation is confined to those roots, so an admin-only endpoint cannot be
  walked into the rest of the container's filesystem;
* **directories only, and writability answered by writing.** A folder that looks fine and
  is mounted read-only is the failure this replaces, so each entry is probed the only way
  that gives a true answer.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# Where browsing may start. Everything reachable is under one of these, so this endpoint
# cannot be used to walk the container's filesystem even by an administrator.
DEFAULT_ROOTS = ("/backups", "/data", "/mnt", "/media", "/srv", "/var/backups")
_PROBE = ".ki-write-probe"


class BrowseError(RuntimeError):
    """The path is outside what may be browsed, or is not a directory."""


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    writable: bool
    empty: bool

    def payload(self) -> dict:
        return {"name": self.name, "path": self.path, "writable": self.writable, "empty": self.empty}


def allowed_roots(configured: str | None = None) -> list[Path]:
    """The volumes this appliance can put backups on, deduplicated and real.

    The configured path is always included even when it is somewhere unusual, so an
    appliance that was set up by hand does not lose the ability to browse its own folder.
    """
    candidates = [Path(item) for item in DEFAULT_ROOTS]
    if configured and configured.strip():
        candidates.append(Path(configured.strip()))
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def places(configured: str | None = None) -> list[dict]:
    """The starting points, with the number that decides between them: free space."""
    found = []
    for root in allowed_roots(configured):
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        writable = _is_writable(root)
        # A base image ships /srv, /media and /var/backups whether or not anything is
        # mounted there. Offering six places when two are usable is a menu that has to be
        # read rather than a choice that can be made, so a directory that is neither
        # writable nor holds anything is left out — it is scaffolding, not a drive.
        if not writable and not _has_children(root) and str(root) != (configured or "").strip():
            continue
        found.append(
            {
                "path": str(root),
                "label": _label(root),
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "writable": writable,
            }
        )
    return found


def _has_children(root: Path) -> bool:
    try:
        return any(item.is_dir() and not item.name.startswith(".") for item in root.iterdir())
    except OSError:
        return False


def _label(root: Path) -> str:
    return {
        "/backups": "Backup volume",
        "/data": "Appliance data",
        "/mnt": "Mounted drives",
        "/media": "Removable media",
    }.get(str(root), str(root))


def listing(path: str, configured: str | None = None) -> dict:
    """What is inside one directory: its sub-folders, and whether it can be written to."""
    target = _resolve_within_roots(path, configured)
    entries: list[Entry] = []
    try:
        for item in sorted(target.iterdir(), key=lambda entry: entry.name.lower()):
            # Directories only. This is a folder picker, and not listing files keeps an
            # admin-only endpoint from becoming a way to read what is in the estate.
            if not item.is_dir() or item.name.startswith("."):
                continue
            entries.append(
                Entry(
                    name=item.name,
                    path=str(item),
                    writable=_is_writable(item),
                    empty=not any(item.iterdir()),
                )
            )
    except PermissionError as exc:
        raise BrowseError(f"this appliance is not allowed to read {target}: {exc}") from exc
    except OSError as exc:
        raise BrowseError(f"could not read {target}: {exc}") from exc

    parent = str(target.parent) if _within_roots(target.parent, configured) else None
    try:
        usage = shutil.disk_usage(target)
        free, total = usage.free, usage.total
    except OSError:
        free = total = None
    return {
        "path": str(target),
        "parent": parent,
        "writable": _is_writable(target),
        "free_bytes": free,
        "total_bytes": total,
        "entries": [entry.payload() for entry in entries],
    }


def create(path: str, name: str, configured: str | None = None) -> dict:
    """Make one sub-folder, so choosing a folder does not mean leaving the page."""
    parent = _resolve_within_roots(path, configured)
    cleaned = (name or "").strip().strip("/")
    if not cleaned or "/" in cleaned or cleaned in (".", ".."):
        raise BrowseError("a folder name cannot be empty, contain a slash, or be '.' or '..'")
    target = parent / cleaned
    try:
        target.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise BrowseError(f"could not create {target}: {exc}") from exc
    return listing(str(parent), configured)


def _is_writable(path: Path) -> bool:
    """Answered by writing. A read-only mount passes every other test."""
    probe = path / f"{_PROBE}-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _resolve_within_roots(path: str, configured: str | None) -> Path:
    if not (path or "").strip():
        raise BrowseError("no folder given")
    try:
        target = Path(path).expanduser().resolve()
    except OSError as exc:
        raise BrowseError(f"{path} cannot be resolved: {exc}") from exc
    if not _within_roots(target, configured):
        raise BrowseError(
            f"{target} is outside the folders this appliance offers for backups. Mount the "
            "drive you want into the container and it will appear here."
        )
    if not target.is_dir():
        raise BrowseError(f"{target} is not a folder")
    return target


def _within_roots(path: Path, configured: str | None) -> bool:
    for root in allowed_roots(configured):
        if path == root or root in path.parents:
            return True
    return False
