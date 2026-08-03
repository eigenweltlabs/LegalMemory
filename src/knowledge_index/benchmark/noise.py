"""Realistic DMS mess for the firm layout — opt-in, deterministic, gold-safe.

A pristine, perfectly-uniform tree lets retrieval and agent navigation lean on structure
that a real document management system never has. This adds structural noise, each matter
seeded so it is messy the same way on every rebuild:

- **flat matters** — every file dumped in the matter root, no workstream folders.
- **alternate taxonomies** — a different-but-valid folder vocabulary (``Emails`` /
  ``Working Papers`` / ``Executed``) instead of the canonical one.
- **system junk** — ``.DS_Store`` / ``Thumbs.db`` cruft and empty ``_To File`` folders.

Document *versions* are deliberately NOT fabricated: Harvey bundles already ship genuine
version material (initial draft, counterparty markup, round-N redlines — different
documents, differently dated, in 31 of 48 matters), so synthetic ``-v2`` copies would be
both fake and redundant. The mess here is only structural + junk.

Invariant: junk gets the matter's ACL grant (so no wall leaks) but is *not* a document,
and the connector quarantines it at extraction — it never reaches the index. Nothing here
can move a gold path across a client wall.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

# a different secretary's equally-valid folder names for the same workstreams
_ALT_TAXONOMY: dict[str, str] = {
    "Correspondence": "Emails",
    "Drafts": "Working Papers",
    "Precedents": "Precedent Bank",
    "Reference": "Client Materials",
    "Schedules": "Schedules & Exhibits",
    "Executed": "Executed",
    "Documents": "Misc",
}
_JUNK_FILES: tuple[str, ...] = (".DS_Store", "Thumbs.db")
_EMPTY_DIRS: tuple[str, ...] = ("_To File", "_Archive", "_Superseded")


@dataclass(frozen=True)
class NoiseConfig:
    """Proportions for one noise level (fractions of matters affected)."""

    flat_rate: float
    alt_rate: float  # applied to the matters not already made flat
    junk_rate: float


LEVELS: dict[str, NoiseConfig] = {
    "light": NoiseConfig(flat_rate=0.18, alt_rate=0.18, junk_rate=0.40),
    "heavy": NoiseConfig(flat_rate=0.28, alt_rate=0.25, junk_rate=0.70),
}


def resolve(level: str | None) -> NoiseConfig | None:
    if not level or level == "none":
        return None
    try:
        return LEVELS[level]
    except KeyError:
        raise ValueError(
            f"unknown noise level {level!r}; use {'/'.join(['none', *LEVELS])}"
        ) from None


def _rng(seed: int, matter_no: str, salt: str) -> random.Random:
    """A stable per-(matter, aspect) RNG so each matter is messy reproducibly."""
    return random.Random(f"{seed}:{matter_no}:{salt}")


def matter_style(cfg: NoiseConfig, seed: int, matter_no: str) -> str:
    """``"flat"`` (no subfolders), ``"alt"`` (renamed folders), or ``"standard"``."""
    roll = _rng(seed, matter_no, "style").random()
    if roll < cfg.flat_rate:
        return "flat"
    if roll < cfg.flat_rate + cfg.alt_rate:
        return "alt"
    return "standard"


def place(workstream: str, style: str) -> str:
    """The subfolder a document lands in under the matter — ``""`` means the matter root."""
    if style == "flat":
        return ""
    if style == "alt":
        return _ALT_TAXONOMY.get(workstream, workstream)
    return workstream


def wants(cfg_rate: float, seed: int, matter_no: str, salt: str) -> bool:
    return _rng(seed, matter_no, salt).random() < cfg_rate


def scatter_junk(matter_dir: Path, seed: int, matter_no: str) -> list[Path]:
    """Create an empty limbo folder and (usually) a cruft file; return the junk *files*.

    Empty folders are inert (the connector skips non-files). Junk files are returned so the
    caller can wall them with the matter's ACL; they fail extraction and are quarantined,
    so they never enter the index.
    """
    rng = _rng(seed, matter_no, "junk")
    (matter_dir / rng.choice(_EMPTY_DIRS)).mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    if rng.random() < 0.7:
        junk = matter_dir / rng.choice(_JUNK_FILES)
        junk.write_bytes(b"\x00\x01\x02\x03")  # not a parseable document
        files.append(junk)
    return files
