from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.db.models import Source, SourceObject, SourceObjectGrant
from knowledge_index.sync import PluginDropSource, SyncEngine

REFERENCE_EXPORT = (
    Path(__file__).resolve().parents[1] / "examples" / "plugins" / "reference_export.py"
)


def _write_drop(root: Path, rows: list[dict], files: dict[str, str]) -> None:
    (root / "files").mkdir(parents=True, exist_ok=True)
    for content_file, text in files.items():
        target = root / "files" / content_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    with (root / "observations.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _add_source(session: Session, root: Path) -> Source:
    source = Source(kind="plugin_drop", display_name="RA-MICRO export", config={"root": str(root)})
    session.add(source)
    session.flush()
    return source


def test_conformant_drop_dir_syncs_with_acls(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    drop = tmp_path / "drop"
    _write_drop(
        drop,
        rows=[
            {
                "schema": "ki-plugin-observation/v1",
                "external_id": "AKTE-2026-42",
                "path": "Mandate/AKTE-2026-42/Vertrag.pdf",
                "name": "Vertrag.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 21,
                "mtime": "2026-07-17T09:00:00Z",
                "content_file": "AKTE-2026-42/Vertrag.pdf",
                "acl": [
                    {"principal": "group:litigation", "principal_kind": "group", "access": "allow"}
                ],
            }
        ],
        files={"AKTE-2026-42/Vertrag.pdf": "Haftung ist begrenzt."},
    )

    connector = PluginDropSource(drop)
    observations = list(connector.full_scan())
    assert len(observations) == 1
    assert observations[0].external_id == "AKTE-2026-42"
    assert isinstance(observations[0].mtime, datetime)
    assert observations[0].mtime.tzinfo is not None
    with connector.fetch("AKTE-2026-42") as stream:
        assert stream.read() == b"Haftung ist begrenzt."

    with factory() as session:
        source = _add_source(session, drop)
        result = SyncEngine(session, source, connector).sync()
        session.commit()
        assert result.created == 1
        obj = session.scalar(select(SourceObject))
        assert obj is not None and obj.external_id == "AKTE-2026-42"
        grant = session.scalar(select(SourceObjectGrant))
        assert grant is not None
        assert (grant.principal, grant.effect) == ("group:litigation", "allow")


def test_deleted_true_tombstones_on_resync(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    drop = tmp_path / "drop"
    base_row = {
        "schema": "ki-plugin-observation/v1",
        "external_id": "AKTE-1",
        "path": "AKTE-1.txt",
        "name": "AKTE-1.txt",
        "content_file": "AKTE-1.txt",
    }
    _write_drop(drop, rows=[base_row], files={"AKTE-1.txt": "content"})

    with factory() as session:
        source = _add_source(session, drop)
        first = SyncEngine(session, source, PluginDropSource(drop)).sync()
        session.commit()
        assert first.created == 1

        # The plugin re-emits the object with an explicit tombstone.
        _write_drop(
            drop,
            rows=[{**base_row, "deleted": True}],
            files={"AKTE-1.txt": "content"},
        )
        second = SyncEngine(session, source, PluginDropSource(drop)).sync()
        session.commit()
        assert second.tombstoned == 1
        obj = session.scalar(select(SourceObject))
        assert obj is not None and obj.deleted_at is not None


def test_malformed_jsonl_line_names_the_line(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    (drop / "files").mkdir(parents=True)
    good = json.dumps(
        {
            "schema": "ki-plugin-observation/v1",
            "external_id": "ok",
            "path": "ok.txt",
            "name": "ok.txt",
            "content_file": "ok.txt",
        }
    )
    with (drop / "observations.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(good + "\n")
        handle.write("\n")
        handle.write("{not valid json\n")

    with pytest.raises(ValueError, match=r"observations\.jsonl line 3"):
        list(PluginDropSource(drop).full_scan())


def test_unknown_schema_marker_is_rejected(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    _write_drop(
        drop,
        rows=[
            {
                "schema": "ki-plugin-observation/v99",
                "external_id": "ok",
                "path": "ok.txt",
                "name": "ok.txt",
                "content_file": "ok.txt",
            }
        ],
        files={"ok.txt": "x"},
    )
    with pytest.raises(ValueError, match="unexpected schema"):
        list(PluginDropSource(drop).full_scan())


def test_content_file_escape_is_rejected_on_fetch(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    secret = tmp_path / "secret.txt"
    secret.write_text("do not leak", encoding="utf-8")
    _write_drop(
        drop,
        rows=[
            {
                "schema": "ki-plugin-observation/v1",
                "external_id": "evil",
                "path": "evil.txt",
                "name": "evil.txt",
                "content_file": "../../secret.txt",
            }
        ],
        files={"placeholder.txt": "x"},
    )
    connector = PluginDropSource(drop)
    list(connector.full_scan())
    with pytest.raises(ValueError, match="escapes|safe relative"):
        connector.fetch("evil")


def test_reference_export_produces_a_syncable_drop_dir(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    input_dir = tmp_path / "input"
    (input_dir / "Mandate").mkdir(parents=True)
    (input_dir / "Mandate" / "Vertrag.txt").write_text("Vertragstext", encoding="utf-8")
    (input_dir / "Notiz.txt").write_text("Kurze Notiz", encoding="utf-8")
    out_dir = tmp_path / "drop"

    completed = subprocess.run(
        [
            sys.executable,
            str(REFERENCE_EXPORT),
            str(input_dir),
            str(out_dir),
            "--group",
            "group:kanzlei",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert (out_dir / "observations.jsonl").is_file()

    connector = PluginDropSource(out_dir)
    external_ids = {obs.external_id for obs in connector.full_scan()}
    assert external_ids == {"Mandate/Vertrag.txt", "Notiz.txt"}
    with connector.fetch("Mandate/Vertrag.txt") as stream:
        assert stream.read() == b"Vertragstext"

    with factory() as session:
        source = _add_source(session, out_dir)
        result = SyncEngine(session, source, connector).sync()
        session.commit()
        assert result.created == 2
        grants = session.scalars(select(SourceObjectGrant)).all()
        assert {g.principal for g in grants} == {"group:kanzlei"}
