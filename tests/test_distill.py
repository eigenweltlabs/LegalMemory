"""Pure-function tests for signal-dense ingestion helpers (no DB, no services)."""

from __future__ import annotations

from knowledge_index.pipeline.distill import (
    build_profile_text,
    context_header,
    contextualize,
)


def test_context_header_joins_present_parts() -> None:
    header = context_header(
        title="Unternehmenskaufvertrag",
        doc_type="share_purchase_agreement",
        matter_title="Projekt Falke",
    )
    assert header == "Unternehmenskaufvertrag — share_purchase_agreement — Projekt Falke"


def test_context_header_skips_missing_and_blank_parts() -> None:
    assert context_header(title="SPA", doc_type=None, matter_title="  ") == "SPA"
    assert context_header(title=None, doc_type="contract", matter_title=None) == "contract"
    assert context_header(title=None, doc_type=None, matter_title=None) == ""


def test_contextualize_prefixes_only_when_header_present() -> None:
    assert contextualize("Der Kaufpreis beträgt 1 EUR.", "SPA — Projekt Falke") == (
        "SPA — Projekt Falke\n\nDer Kaufpreis beträgt 1 EUR."
    )
    assert contextualize("The purchase price is 1 EUR.", "") == "The purchase price is 1 EUR."


def test_build_profile_text_includes_all_provided_fields_and_truncates() -> None:
    text = "Präambel. " + "x" * 1000
    profile = build_profile_text(
        title="Unternehmenskaufvertrag",
        doc_type="share_purchase_agreement",
        matter_title="Projekt Falke",
        reference_numbers=["M-2026-0042"],
        parties=["Alpha GmbH", "Beta AG"],
        identifiers=["§ 433 BGB", "HRB 12345"],
        doc_date="2026-03-14",
        text=text,
        excerpt_chars=400,
    )
    # English labels; the values stay in the document's language
    assert "Title: Unternehmenskaufvertrag" in profile
    assert "Document type: share_purchase_agreement" in profile
    assert "Matter: Projekt Falke" in profile
    assert "Reference numbers: M-2026-0042" in profile
    assert "Parties: Alpha GmbH, Beta AG" in profile
    assert "Identifiers: § 433 BGB, HRB 12345" in profile
    assert "Date: 2026-03-14" in profile
    assert "Excerpt: Präambel." in profile
    # the excerpt is capped: only ~400 chars of the padded body survive
    assert profile.count("x") <= 400
    assert "x" * 500 not in profile


def test_build_profile_text_skips_empty_fields() -> None:
    profile = build_profile_text(
        title="Kaufvertrag",
        doc_type=None,
        matter_title=None,
        reference_numbers=[],
        parties=[],
        identifiers=[],
        doc_date=None,
        text="",
    )
    assert profile == "Title: Kaufvertrag"


def test_build_profile_text_collapses_excerpt_whitespace() -> None:
    profile = build_profile_text(
        title=None,
        doc_type=None,
        matter_title=None,
        reference_numbers=[],
        parties=[],
        identifiers=[],
        doc_date=None,
        text="line one\n\n   line   two\t\tend",
    )
    assert profile == "Excerpt: line one line two end"
