"""Billing extraction: dedup, LEDES/UTBMS rows, and entity resolution — no live model."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    BillingInvoice,
    BillingLineItem,
    Blob,
    Client,
    Document,
    DocumentVersion,
    EntityIdentifier,
)
from knowledge_index.pipeline import billing as billing_module
from knowledge_index.pipeline.billing import BillingExtractor, resolve_entity
from knowledge_index.pipeline.extraction import BillingExtraction, InvoiceLine


def _seed_invoice_document(session: Session) -> None:
    session.add(Blob(content_hash="h1", size_bytes=8))
    session.flush()
    session.add(Artifact(content_hash="h1", producer="p", producer_version="v1", kind="structured_json", payload={"text": "Rechnung"}))
    document = Document(id="doc-1", title="Rechnung 2026-001", doc_type="invoice", matter_id=None)
    session.add(document)
    session.flush()
    version = DocumentVersion(
        id="ver-1", document_id="doc-1", ordinal=1, status="final", content_hash="h1"
    )
    session.add(version)
    document.latest_final_version_id = "ver-1"
    session.commit()


def _stub_generation() -> BillingExtraction:
    return BillingExtraction(
        is_invoice=True,
        invoice_number="2026-001",
        invoice_date="2026-03-15",
        currency="EUR",
        invoice_total=1900.0,
        lines=[
            InvoiceLine(line_item_number=1, line_type="F", task_code="L110", activity_code="A101", units=2.0, unit_cost=400.0, line_total=800.0, timekeeper_name="Dr. Muster", description="Prüfung"),
            InvoiceLine(line_item_number=2, line_type="F", task_code="L120", units=2.5, unit_cost=400.0, line_total=1000.0, timekeeper_name="Dr. Muster"),
            InvoiceLine(line_item_number=2, line_type="F", task_code="L120", units=2.5, unit_cost=400.0, line_total=1000.0, timekeeper_name="Dr. Muster"),
        ],
    )


def test_billing_extractor_dedups_and_stores_ledes_rows(
    session: Session, factory: sessionmaker, tmp_path: Path, monkeypatch
) -> None:
    _seed_invoice_document(session)
    monkeypatch.setattr(billing_module, "chat_json", lambda *a, **k: _stub_generation())

    result = BillingExtractor(factory, AppConfig(artifact_dir=tmp_path)).extract()
    assert result.invoices_inserted == 1
    # the duplicate line_item_number 2 is collapsed
    assert result.line_items_inserted == 2

    with factory() as verify:
        invoice = verify.scalar(select(BillingInvoice))
        assert invoice.invoice_number == "2026-001" and invoice.currency == "EUR"
        lines = verify.scalars(select(BillingLineItem)).all()
        assert {line.task_code for line in lines} == {"L110", "L120"}
        # one timekeeper reused across both lines
        assert len({line.timekeeper_id for line in lines}) == 1

    # Re-running does not duplicate the invoice (unique invoice_number per firm).
    again = BillingExtractor(factory, AppConfig(artifact_dir=tmp_path)).extract()
    assert again.invoices_inserted == 0
    with factory() as verify:
        assert verify.scalar(select(func.count()).select_from(BillingInvoice)) == 1


def test_resolve_entity_by_name_and_identifier(session: Session) -> None:
    client = Client(id="c-1", name="Muster GmbH", kind="company", identifiers={"de_hrb": "HRB 12345"})
    session.add(client)
    session.flush()
    session.add(
        EntityIdentifier(entity_type="client", entity_id="c-1", scheme="de_hrb", value="HRB 12345")
    )
    session.commit()

    by_name = resolve_entity(session, "Muster")
    assert by_name and by_name[0]["id"] == "c-1" and by_name[0]["matched_by"] == "name"
    by_id = resolve_entity(session, "HRB 12345")
    assert by_id and by_id[0]["id"] == "c-1"
    assert any(item["scheme"] == "de_hrb" for item in by_id[0]["identifiers"])
