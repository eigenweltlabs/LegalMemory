"""Structured billing extraction: LEDES/UTBMS-aligned invoices out of indexed documents.

Billing is NOT document metadata and is NOT chunked into the search index — it is
relational data. This extractor scans invoice documents that flow through the connector,
extracts their line items via the model (deterministic field mapping, no regex), and
inserts deduplicated rows: an invoice is unique per (law_firm_id, invoice_number); a line
is unique per (invoice, line_item_number); a timekeeper per (law_firm_id, ledes id). It
also promotes the free-form client/party identifiers into typed EntityIdentifier rows so an
agent can resolve entities by LEI / HRB / VAT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_index.config import AppConfig
from knowledge_index.db.models import (
    Artifact,
    BillingInvoice,
    BillingLineItem,
    Client,
    Document,
    DocumentVersion,
    DocumentVersionSource,
    EntityIdentifier,
    MatterClient,
    Party,
    Timekeeper,
)
from knowledge_index.pipeline.extraction import BILLING_SYSTEM, BillingExtraction
from knowledge_index.pipeline.providers import chat_json, usage_stage

BILLING_DOC_TYPES = ("invoice",)
LAW_FIRM_ID = "self"


@dataclass
class BillingResult:
    considered: int = 0
    invoices_inserted: int = 0
    line_items_inserted: int = 0
    duplicates_skipped: int = 0
    not_invoices: int = 0
    identifiers_promoted: int = 0
    errors: int = 0


def _iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _final_text(session: Session, content_hash: str | None) -> str:
    if not content_hash:
        return ""
    artifact = session.scalar(
        select(Artifact)
        .where(Artifact.content_hash == content_hash, Artifact.kind == "structured_json")
        .order_by(Artifact.created_at.desc())
    )
    return str((artifact.payload or {}).get("text") or "") if artifact else ""


def _get_or_create_timekeeper(session: Session, ledes_id: str, name: str | None) -> str:
    existing = session.scalar(
        select(Timekeeper).where(
            Timekeeper.law_firm_id == LAW_FIRM_ID, Timekeeper.ledes_timekeeper_id == ledes_id
        )
    )
    if existing is not None:
        if name and not existing.name:
            existing.name = name
        return existing.id
    timekeeper = Timekeeper(law_firm_id=LAW_FIRM_ID, ledes_timekeeper_id=ledes_id, name=name)
    session.add(timekeeper)
    session.flush()
    return timekeeper.id


def promote_entity_identifiers(session: Session) -> int:
    """Promote free-form client/party ``identifiers`` JSON into typed EntityIdentifier rows."""
    promoted = 0
    for entity_type, model in (("client", Client), ("party", Party)):
        for entity in session.scalars(select(model)):
            identifiers = entity.identifiers or {}
            items = identifiers.items() if isinstance(identifiers, dict) else []
            for scheme, value in items:
                if not value:
                    continue
                scheme_key = str(scheme).strip().lower()
                value_str = str(value).strip()
                exists = session.scalar(
                    select(EntityIdentifier).where(
                        EntityIdentifier.entity_type == entity_type,
                        EntityIdentifier.entity_id == entity.id,
                        EntityIdentifier.scheme == scheme_key,
                        EntityIdentifier.value == value_str,
                    )
                )
                if exists is not None:
                    continue
                session.add(
                    EntityIdentifier(
                        entity_type=entity_type,
                        entity_id=entity.id,
                        scheme=scheme_key,
                        value=value_str,
                        provenance={"source": "identifier-promotion"},
                    )
                )
                promoted += 1
    return promoted


class BillingExtractor:
    def __init__(self, session_factory: sessionmaker, config: AppConfig) -> None:
        self.session_factory = session_factory
        self.config = config

    def extract(self, *, limit: int | None = None) -> BillingResult:
        result = BillingResult()
        slot = self.config.models.extract
        with self.session_factory() as session:
            result.identifiers_promoted = promote_entity_identifiers(session)

            documents = session.scalars(
                select(Document).where(Document.doc_type.in_(BILLING_DOC_TYPES))
            ).all()
            for document in documents:
                if limit is not None and result.invoices_inserted >= limit:
                    break
                version = (
                    session.get(DocumentVersion, document.latest_final_version_id)
                    if document.latest_final_version_id
                    else None
                )
                if version is None:
                    continue
                source_object_id = session.scalar(
                    select(DocumentVersionSource.source_object_id).where(
                        DocumentVersionSource.version_id == version.id
                    )
                )
                # already extracted from this document?
                if source_object_id and session.scalar(
                    select(BillingInvoice).where(
                        BillingInvoice.source_object_id == source_object_id
                    )
                ):
                    continue

                result.considered += 1
                try:
                    with usage_stage("extract_billing"):
                        extracted = chat_json(
                            slot,
                            self.config,
                            system=BILLING_SYSTEM,
                            user=_final_text(session, version.content_hash)[:16000],
                            schema=BillingExtraction,
                            max_output_tokens=3000,
                        )
                except Exception:
                    result.errors += 1
                    continue

                if not extracted.is_invoice or not extracted.invoice_number:
                    result.not_invoices += 1
                    continue

                # Dedup: an invoice number is unique per firm.
                if session.scalar(
                    select(BillingInvoice).where(
                        BillingInvoice.law_firm_id == LAW_FIRM_ID,
                        BillingInvoice.invoice_number == extracted.invoice_number,
                    )
                ):
                    result.duplicates_skipped += 1
                    continue

                client_id = session.scalar(
                    select(MatterClient.client_id).where(
                        MatterClient.matter_id == document.matter_id
                    )
                ) if document.matter_id else None
                invoice = BillingInvoice(
                    law_firm_id=LAW_FIRM_ID,
                    invoice_number=extracted.invoice_number,
                    invoice_date=_iso_date(extracted.invoice_date),
                    client_id=client_id,
                    matter_id=document.matter_id,
                    invoice_total=extracted.invoice_total,
                    tax_total=extracted.tax_total,
                    currency=extracted.currency,
                    source_object_id=source_object_id,
                    provenance={"model": slot.model, "source": "billing-extractor"},
                )
                session.add(invoice)
                session.flush()
                result.invoices_inserted += 1

                seen_lines: set[int] = set()
                for line in extracted.lines:
                    if line.line_item_number in seen_lines:
                        continue
                    seen_lines.add(line.line_item_number)
                    timekeeper_id = None
                    if line.timekeeper_id or line.timekeeper_name:
                        timekeeper_id = _get_or_create_timekeeper(
                            session,
                            line.timekeeper_id or line.timekeeper_name or "unknown",
                            line.timekeeper_name,
                        )
                    session.add(
                        BillingLineItem(
                            invoice_id=invoice.id,
                            line_item_number=line.line_item_number,
                            line_type=line.line_type,
                            line_item_date=_iso_date(line.date),
                            timekeeper_id=timekeeper_id,
                            task_code=line.task_code,
                            activity_code=line.activity_code,
                            expense_code=line.expense_code,
                            number_of_units=line.units,
                            unit_cost=line.unit_cost,
                            line_item_total=line.line_total,
                            description=line.description,
                            provenance={"source": "billing-extractor"},
                        )
                    )
                    result.line_items_inserted += 1

            session.commit()
        return result


def billing_rollup(session: Session, matter_id: str) -> dict:
    """Aggregate billing for a matter: totals and per-UTBMS-task hours/fees."""
    invoices = session.scalars(
        select(BillingInvoice).where(BillingInvoice.matter_id == matter_id)
    ).all()
    invoice_ids = [invoice.id for invoice in invoices]
    total = sum(invoice.invoice_total or 0.0 for invoice in invoices)
    by_task: dict[str, dict[str, float]] = {}
    if invoice_ids:
        rows = session.execute(
            select(
                BillingLineItem.task_code,
                func.sum(BillingLineItem.number_of_units),
                func.sum(BillingLineItem.line_item_total),
            )
            .where(BillingLineItem.invoice_id.in_(invoice_ids))
            .group_by(BillingLineItem.task_code)
        ).all()
        for task_code, units, amount in rows:
            by_task[task_code or "uncoded"] = {
                "units": float(units or 0.0),
                "amount": float(amount or 0.0),
            }
    return {
        "matter_id": matter_id,
        "invoice_count": len(invoices),
        "invoiced_total": total,
        "currency": next((invoice.currency for invoice in invoices if invoice.currency), None),
        "by_task_code": by_task,
    }


def resolve_entity(session: Session, needle: str) -> list[dict]:
    """Resolve a name or identifier to clients/parties, including shared-identifier matches."""
    needle = (needle or "").strip()
    if not needle:
        return []
    matches: dict[tuple[str, str], dict] = {}

    def _add(entity_type: str, entity, reason: str) -> None:
        key = (entity_type, entity.id)
        if key not in matches:
            matches[key] = {
                "entity_type": entity_type,
                "id": entity.id,
                "name": entity.name,
                "kind": entity.kind,
                "matched_by": reason,
                "identifiers": [
                    {"scheme": row.scheme, "value": row.value}
                    for row in session.scalars(
                        select(EntityIdentifier).where(
                            EntityIdentifier.entity_type == entity_type,
                            EntityIdentifier.entity_id == entity.id,
                        )
                    )
                ],
            }

    for entity_type, model in (("client", Client), ("party", Party)):
        for entity in session.scalars(select(model).where(model.name.ilike(f"%{needle}%")).limit(20)):
            _add(entity_type, entity, "name")
    for identifier in session.scalars(
        select(EntityIdentifier).where(EntityIdentifier.value.ilike(f"%{needle}%")).limit(20)
    ):
        model = Client if identifier.entity_type == "client" else Party
        entity = session.get(model, identifier.entity_id)
        if entity is not None:
            _add(identifier.entity_type, entity, f"identifier:{identifier.scheme}")
    return list(matches.values())
