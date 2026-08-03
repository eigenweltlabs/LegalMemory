#!/usr/bin/env python3
"""Reference connector plugin: export a folder to a Knowledge Index drop directory.

The contract (full spec: docs/connector-plugins.md):
1. Emit <out>/observations.jsonl, one JSON object per line, first field
   "schema": "ki-plugin-observation/v1".
2. Each row: external_id (stable id), path, name, content_file (relative to
   <out>/files/), and optionally mime_type, size_bytes, mtime, acl, deleted.
3. Copy each object's bytes to <out>/files/<content_file>.
4. Absent acl means unknown -> the engine denies by default; pass --group to grant.
5. Re-run to refresh; emit {"deleted": true} rows (or just omit) to tombstone.
6. No Knowledge Index code changes: copy this file per customer, swap the walk for
   your DMS export (RA-MICRO / AnNoText / Advoware / SQL), keep the output shape.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_MARKER = "ki-plugin-observation/v1"


def build_drop_dir(input_dir: Path, out_dir: Path, group: str | None) -> int:
    files_dir = out_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    acl = None
    if group:
        acl = [{"principal": group, "principal_kind": "group", "access": "allow"}]

    count = 0
    with (out_dir / "observations.jsonl").open("w", encoding="utf-8") as handle:
        for path in sorted(input_dir.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink() or not path.is_file():
                continue
            # external_id is the stable relative POSIX path; also the content_file name.
            relative = path.relative_to(input_dir).as_posix()
            destination = files_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

            stat = path.stat()
            row = {
                "schema": SCHEMA_MARKER,
                "external_id": relative,
                "path": relative,
                "name": path.name,
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "content_file": relative,
            }
            if acl is not None:
                row["acl"] = acl
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input_dir", type=Path, help="Folder to export")
    parser.add_argument("out_dir", type=Path, help="Drop directory to write")
    parser.add_argument(
        "--group",
        default=None,
        help="Grant read access to this principal (e.g. group:litigation). "
        "Omit to leave ACL unknown (engine denies by default).",
    )
    args = parser.parse_args()

    if not args.input_dir.is_dir():
        parser.error(f"input_dir is not a directory: {args.input_dir}")
    written = build_drop_dir(args.input_dir.resolve(), args.out_dir, args.group)
    print(f"wrote {written} observations to {args.out_dir / 'observations.jsonl'}")


if __name__ == "__main__":
    main()
