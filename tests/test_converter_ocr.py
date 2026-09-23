from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from knowledge_index.config import AppConfig, PipelineConfig
from knowledge_index.db.models import Artifact, Blob, ProcessingState, Source, SourceObject
from knowledge_index.pipeline import PipelineRunner
from knowledge_index.pipeline import converters
from knowledge_index.pipeline.runner import _required_artifact


class _Response:
    """The narrow slice of httpx.Response that _convert_docling reads."""

    status_code = 200
    text = ""

    def __init__(self, text_content: str = "Pozew o zapłatę.") -> None:
        self.text_content = text_content

    def json(self) -> dict:
        return {
            "status": "success",
            "document": {"text_content": self.text_content},
        }

    def raise_for_status(self) -> None:
        return None


@pytest.fixture
def posted(monkeypatch) -> dict:
    """Capture the form Docling Serve is called with, without calling it."""
    captured: dict = {}

    def fake_post(url, *, files, data, timeout):
        captured["url"] = url
        captured["data"] = data
        captured.setdefault("calls", []).append(data)
        return _Response(captured.get("text", "Pozew o zapłatę."))

    monkeypatch.setattr(converters.httpx, "post", fake_post)
    return captured


def _convert(config: AppConfig, tmp_path: Path) -> None:
    scan = tmp_path / "pozew.pdf"
    scan.write_bytes(b"%PDF-1.4 scanned filing")
    converters.convert_document(scan, name=scan.name, mime_type="application/pdf", config=config)


def test_the_deployments_ocr_languages_reach_docling(posted, tmp_path) -> None:
    """A jurisdiction outside de/en has to be able to say so. Before this was a
    setting the pair was compiled in, and a Polish scan was OCR'd with the German
    and English models — which does not fail, it returns confident nonsense that
    the rest of the pipeline then classifies, types and embeds."""
    config = AppConfig(pipeline=PipelineConfig(ocr_languages=["pl", "en"]))

    _convert(config, tmp_path)

    assert posted["data"]["ocr_lang"] == ["pl", "en"]


def test_the_default_stays_the_pair_the_appliance_shipped_with(posted, tmp_path) -> None:
    """Existing deployments must not silently change model set on upgrade."""
    _convert(AppConfig(), tmp_path)

    assert posted["data"]["ocr_lang"] == ["de", "en"]
    assert posted["data"]["ocr_engine"] == "easyocr"


def test_languages_are_normalized_and_deduplicated() -> None:
    """Casing and stray whitespace come from hand-edited config and environment
    variables; easyocr matches its model names exactly."""
    config = PipelineConfig(ocr_languages=[" PL ", "en", "pl"])

    assert config.ocr_languages == ["pl", "en"]


@pytest.mark.parametrize("value", [[], [""], ["  "]])
def test_an_empty_language_set_is_refused(value: list[str]) -> None:
    """Silently falling back to a default here would OCR the estate in the wrong
    language and report success."""
    with pytest.raises(ValueError):
        PipelineConfig(ocr_languages=value)


def test_the_environment_can_pin_the_language_set(monkeypatch) -> None:
    """`KI_PIPELINE__OCR_LANGUAGES`, like every other scalar under pipeline.*."""
    monkeypatch.setenv("KI_PIPELINE__OCR_LANGUAGES", json.dumps(["pl", "de", "en"]))

    assert AppConfig().pipeline.ocr_languages == ["pl", "de", "en"]


def test_rerunning_conversion_refreshes_ocr_and_preserves_current_cache(
    factory, posted, tmp_path
) -> None:
    """Exercise the UI's version bump through requeue, claims and stored artifacts."""
    scan = tmp_path / "pozew.pdf"
    scan.write_bytes(b"%PDF-1.4 scanned filing")
    content_hash = sha256(scan.read_bytes()).hexdigest()
    config = AppConfig(artifact_dir=tmp_path / "artifacts")
    with factory() as session:
        source = Source(kind="local_fs", display_name="scans", config={"root": str(tmp_path)})
        session.add(source)
        session.flush()
        source_object = SourceObject(
            source_id=source.id,
            external_id=scan.name,
            path=str(scan),
            name=scan.name,
            mime_type="application/pdf",
            content_hash=content_hash,
        )
        session.add(source_object)
        session.add(
            Blob(
                content_hash=content_hash,
                size_bytes=scan.stat().st_size,
                cached_path=str(scan),
            )
        )
        session.flush()
        object_id = source_object.id
        session.add(ProcessingState(source_object_id=object_id, stage="convert"))
        session.add(
            ProcessingState(
                source_object_id=object_id,
                stage="classify_matter",
                status="done",
                producer_version=config.pipeline.stage("classify_matter").producer_version,
            )
        )
        session.commit()

    runner = PipelineRunner(factory, config)
    posted["text"] = "Old OCR output"
    assert runner.run_stage_for_object(stage="convert", source_object_id=object_id).done == 1
    original_version = config.pipeline.stage("convert").producer_version

    # A language edit alone keeps existing conversions until the operator asks to rerun.
    config.pipeline.ocr_languages = ["pl", "en"]
    assert runner.requeue_outdated_stages() == 0
    config.pipeline.stage("convert").rerun_token = "1"
    config = AppConfig.model_validate(config.model_dump())
    runner = PipelineRunner(factory, config)
    assert runner.requeue_outdated_stages() == 1
    with factory() as session:
        downstream = session.scalar(
            select(ProcessingState).where(
                ProcessingState.source_object_id == object_id,
                ProcessingState.stage == "classify_matter",
            )
        )
        assert downstream.status == "skipped"
        assert downstream.last_error == {"reason": "waiting_for_previous_stage"}

    posted["text"] = "Pozew o zapłatę."
    assert runner.run_stage_for_object(stage="convert", source_object_id=object_id).done == 1
    assert [call["ocr_lang"] for call in posted["calls"]] == [["de", "en"], ["pl", "en"]]
    with factory() as session:
        artifacts = session.scalars(
            select(Artifact).where(
                Artifact.content_hash == content_hash,
                Artifact.kind == "structured_json",
            )
        ).all()
        assert {artifact.producer_version: artifact.payload["text"] for artifact in artifacts} == {
            original_version: "Old OCR output",
            config.pipeline.stage("convert").producer_version: "Pozew o zapłatę.",
        }
        assert _required_artifact(session, content_hash, "structured_json").payload["text"] == (
            "Pozew o zapłatę."
        )
        downstream = session.scalar(
            select(ProcessingState).where(
                ProcessingState.source_object_id == object_id,
                ProcessingState.stage == "classify_matter",
            )
        )
        assert downstream.status == "pending"

        # A duplicate observation of the same bytes shares the current conversion.
        duplicate = SourceObject(
            source_id=source.id,
            external_id="copy",
            path="copy.pdf",
            name="copy.pdf",
            mime_type="application/pdf",
            content_hash=content_hash,
        )
        session.add(duplicate)
        session.flush()
        duplicate_id = duplicate.id
        session.add(ProcessingState(source_object_id=duplicate_id, stage="convert"))
        session.commit()
    assert runner.run_stage_for_object(stage="convert", source_object_id=duplicate_id).done == 1
    assert len(posted["calls"]) == 2
