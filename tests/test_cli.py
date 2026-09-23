"""`ki status` must answer, not raise.

A second `from sqlalchemy import select` inside the `profile-matters` branch made
`select` local to all of `main()`, so every other branch that used it hit an unbound
name. `ki status` is what an operator runs when the pipeline looks stuck; it raised
`UnboundLocalError` on every call. Neither lint nor the suite noticed, because nothing
ran the command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index import cli
from knowledge_index.db import engine


def test_status_prints_the_stage_counts(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.conftest import TEST_DATABASE_URL

    monkeypatch.setenv("KI_DATABASE_URL", TEST_DATABASE_URL)
    # get_engine() caches the first engine it builds; start from none so it reads the
    # URL above rather than whatever an earlier test left behind.
    monkeypatch.setattr(engine, "_engine", None)
    monkeypatch.setattr(sys, "argv", ["ki", "--config", str(tmp_path / "config.json"), "status"])

    cli.main()

    assert json.loads(capsys.readouterr().out) == []
