"""Conversion layer: Docling Serve owns document understanding (PDF, Office, images,
OCR); plain text is read directly and RFC-822 mail is parsed with the stdlib. DOCX
additionally gets a raw-OOXML pass because Docling flattens tracked changes and we
need the redlines. Anything Docling cannot convert is quarantined — never degraded."""

from __future__ import annotations

import email
import json
import zipfile
from dataclasses import dataclass, field
from email import policy
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

from knowledge_index.config import AppConfig


class UnsupportedDocument(ValueError):
    pass


@dataclass
class ConvertedDocument:
    text: str
    metadata: dict = field(default_factory=dict)
    revisions: list[dict] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "text": self.text,
            "metadata": self.metadata,
            "revisions": self.revisions,
        }


def convert_document(
    path: Path,
    *,
    name: str,
    mime_type: str | None,
    config: AppConfig,
) -> ConvertedDocument:
    suffix = Path(name).suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"} or (
        mime_type or ""
    ).startswith("text/"):
        return _convert_text(path, name=name)
    if suffix == ".eml" or mime_type == "message/rfc822":
        return _convert_eml(path)
    return _convert_docling(path, name=name, mime_type=mime_type, config=config)


def _convert_docling(
    path: Path, *, name: str, mime_type: str | None, config: AppConfig
) -> ConvertedDocument:
    data = {
        "to_formats": ["text", "json"],
        "image_export_mode": "placeholder",
        "include_images": "false",
        "do_ocr": "true",
        "force_ocr": "false",
        # ocr_engine must be pinned: the "auto" default routes to RapidOCR whose
        # constructor ignores ocr_lang entirely — German scans would be OCR'd with
        # the wrong model set while reporting success.
        "ocr_engine": "easyocr",
        "ocr_lang": ["de", "en"],
        "table_mode": "accurate",
        "abort_on_error": "false",
        "document_timeout": "270",
    }
    with path.open("rb") as stream:
        response = httpx.post(
            f"{config.components.docling_url.rstrip('/')}/v1/convert/file",
            files={"files": (name, stream, mime_type or "application/octet-stream")},
            data=data,
            timeout=300,
        )
    if 400 <= response.status_code < 500:
        raise UnsupportedDocument(
            f"docling rejected {name}: {response.status_code} {response.text[:500]}"
        )
    response.raise_for_status()  # 5xx/network errors stay retryable
    payload = response.json()
    status = payload.get("status")
    errors = payload.get("errors") or []
    # partial_success with errors means truncated content; indexing half a
    # Schriftsatz silently would be a hidden degradation — quarantine instead.
    if status != "success" and not (status == "partial_success" and not errors):
        raise UnsupportedDocument(
            f"docling could not fully convert {name}: {status} "
            f"{'; '.join(str(item) for item in errors)[:500]}"
        )
    document = payload.get("document") or {}
    text = document.get("text_content") or document.get("md_content") or ""
    revisions: list[dict] = []
    comments: list[dict] = []
    if Path(name).suffix.casefold() == ".docx":
        ooxml = extract_docx_revisions(path)
        revisions = ooxml.revisions
        comments = ooxml.metadata.get("comments", [])
    return ConvertedDocument(
        text=str(text),
        metadata={
            "converter": "docling-serve",
            "docling_json": document.get("json_content"),
            "status": payload.get("status"),
            "errors": payload.get("errors") or [],
            "comments": comments,
        },
        revisions=revisions,
    )


def _convert_text(path: Path, *, name: str) -> ConvertedDocument:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 always decodes
        text = raw.decode("utf-8", errors="replace")
    metadata = {"filename": name, "converter": "builtin-text"}
    if name.lower().endswith(".json"):
        try:
            metadata["json"] = json.loads(text)
        except json.JSONDecodeError:
            pass
    return ConvertedDocument(text=text, metadata=metadata)


def _convert_eml(path: Path) -> ConvertedDocument:
    message = email.message_from_binary_file(path.open("rb"), policy=policy.default)
    bodies: list[str] = []
    attachments: list[dict] = []
    if message.is_multipart():
        for part in message.walk():
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                attachments.append(
                    {
                        "filename": part.get_filename(),
                        "content_type": part.get_content_type(),
                    }
                )
            elif part.get_content_type() == "text/plain":
                bodies.append(str(part.get_content()))
    else:
        bodies.append(str(message.get_content()))
    metadata = {
        "converter": "stdlib-email",
        "subject": message.get("subject"),
        "from": message.get("from"),
        "to": message.get("to"),
        "cc": message.get("cc"),
        "date": message.get("date"),
        "message_id": message.get("message-id"),
        "in_reply_to": message.get("in-reply-to"),
        "references": message.get("references"),
        "attachments": attachments,
    }
    headers = "\n".join(
        f"{label}: {metadata[key]}"
        for label, key in (("Betreff", "subject"), ("Von", "from"), ("An", "to"))
        if metadata[key]
    )
    return ConvertedDocument(text=f"{headers}\n\n" + "\n\n".join(bodies), metadata=metadata)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx_revisions(path: Path) -> ConvertedDocument:
    """Raw-OOXML pass for what Docling flattens: tracked changes and comments."""
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
            comments = _docx_comments(archive)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise UnsupportedDocument(f"invalid DOCX: {exc}") from exc

    paragraphs: list[str] = []
    revisions: list[dict] = []
    for paragraph in root.iter(f"{W}p"):
        accepted: list[str] = []
        for element in paragraph.iter():
            tag = element.tag
            if tag in {f"{W}ins", f"{W}del", f"{W}moveFrom", f"{W}moveTo"}:
                kind = tag.removeprefix(W)
                revision_text = "".join(
                    child.text or ""
                    for child in element.iter()
                    if child.tag in {f"{W}t", f"{W}delText"}
                )
                if revision_text:
                    revisions.append(
                        {
                            "kind": kind,
                            "text": revision_text,
                            "author": element.attrib.get(f"{W}author"),
                            "date": element.attrib.get(f"{W}date"),
                        }
                    )
            if tag == f"{W}t" and not _inside_deleted(element, paragraph):
                accepted.append(element.text or "")
            elif tag == f"{W}tab":
                accepted.append("\t")
            elif tag == f"{W}br":
                accepted.append("\n")
        value = "".join(accepted).strip()
        if value:
            paragraphs.append(value)
    return ConvertedDocument(
        text="\n\n".join(paragraphs),
        metadata={"converter": "builtin-ooxml", "comments": comments},
        revisions=revisions,
    )


def _inside_deleted(element: ET.Element, paragraph: ET.Element) -> bool:
    # ElementTree has no parent pointers. Deletion text uses w:delText, so normal w:t
    # is accepted in the OOXML produced by our fixtures and common Word versions.
    return element.tag == f"{W}delText"


def _docx_comments(archive: zipfile.ZipFile) -> list[dict]:
    try:
        root = ET.fromstring(archive.read("word/comments.xml"))
    except (KeyError, ET.ParseError):
        return []
    return [
        {
            "id": item.attrib.get(f"{W}id"),
            "author": item.attrib.get(f"{W}author"),
            "date": item.attrib.get(f"{W}date"),
            "text": "".join(node.text or "" for node in item.iter(f"{W}t")),
        }
        for item in root.iter(f"{W}comment")
    ]


