from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from pipeline.common.ids import new_id
from pipeline.common.schemas import (
    Bubble,
    Check,
    Cited,
    ModelRef,
    Page,
    Provenance,
    RightsDenied,
    RightsRecord,
    SceneJSON,
    Shot,
    ValidationReport,
    Voice,
    VoiceLine,
    VoiceLocked,
    require_cloud_allowed,
    require_render_allowed,
    require_voice_usable,
)
from pipeline.common.schemas.source import OCR_CONFIDENCE_MIN

NOW = datetime(2026, 11, 18, tzinfo=UTC)
REF = "media/sha256/" + "a" * 64 + ".png"
PROV: dict[str, Any] = {
    "stage": "understand",
    "tool": "test-vlm",
    "tool_version": "0",
    "license": "Apache-2.0",
    "provider": "none",
    "params_hash": "0" * 64,
    "created_at": NOW.isoformat(),
}


def j(obj: dict[str, Any]) -> str:
    return json.dumps(obj)


# --- ids ---------------------------------------------------------------------------------------


def test_new_id_prefix_and_uniqueness() -> None:
    a, b = new_id("panel"), new_id("panel")
    assert a.startswith("panel_") and a != b
    with pytest.raises(ValueError):
        new_id("nope")


# --- Cited ---------------------------------------------------------------------------------------


def test_cited_value_requires_panel() -> None:
    with pytest.raises(ValidationError, match="cite at least one"):
        Cited[str].model_validate_json(j({"value": "dusk", "panel_ids": []}))


def test_cited_null_is_valid_and_cannot_carry_confidence() -> None:
    assert not Cited[str].model_validate_json(j({})).known
    with pytest.raises(ValidationError):
        Cited[str].model_validate_json(j({"value": None, "confidence": 0.5}))


def test_strict_rejects_unknown_keys_and_coercion() -> None:
    with pytest.raises(ValidationError):
        Cited[str].model_validate_json(j({"value": "x", "panel_ids": ["panel_1"], "extra": 1}))
    with pytest.raises(ValidationError):
        Cited[str].model_validate({"value": 5, "panel_ids": ["panel_1"]})


# --- SceneJSON -----------------------------------------------------------------------------------


def scene(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "scene_1",
        "provenance": PROV,
        "panel_id": "panel_0144",
        "setting": {"value": "Clock chamber, gears", "panel_ids": ["panel_0144"]},
        "action": {"value": "The knight kneels", "panel_ids": ["panel_0144"]},
        "shot_hint": {"value": "two_shot", "panel_ids": ["panel_0144"]},
        "time_of_day": {},
        "weather": {},
        "characters_present": [{"value": "char_ada", "panel_ids": ["panel_0144"]}],
        "emotion_by_character": {"char_ada": {"value": "surprised", "panel_ids": ["panel_0144"]}},
    }
    base.update(over)
    return base


def test_scene_unknown_fields_listed() -> None:
    s = SceneJSON.model_validate_json(j(scene()))
    assert s.unknown_fields == ["time_of_day", "weather"]


def test_scene_field_must_cite_own_panel() -> None:
    bad = scene(setting={"value": "Tower", "panel_ids": ["panel_0001"]})
    with pytest.raises(ValidationError, match="must cite its own panel"):
        SceneJSON.model_validate_json(j(bad))


def test_scene_emotion_only_for_present_characters() -> None:
    bad = scene(emotion_by_character={"char_holt": {"value": "stern", "panel_ids": ["panel_0144"]}})
    with pytest.raises(ValidationError, match="not present"):
        SceneJSON.model_validate_json(j(bad))


def test_scene_rejects_unlisted_shot_hint() -> None:
    with pytest.raises(ValidationError):
        SceneJSON.model_validate_json(
            j(scene(shot_hint={"value": "drone_flyover", "panel_ids": ["panel_0144"]}))
        )


# --- Bubble --------------------------------------------------------------------------------------


def bubble(conf: float, speaker: dict[str, Any] | None = None) -> str:
    return j(
        {
            "id": "bubble_1",
            "provenance": PROV,
            "panel_id": "panel_0144",
            "bbox": {"x": 0, "y": 0, "w": 10, "h": 10},
            "kind": "speech",
            "text": "…I promised.",
            "lang": "en",
            "ocr_confidence": conf,
            "ocr_engine": "paddleocr",
            "speaker": speaker if speaker is not None else {},
            "reading_order": 1,
        }
    )


def test_bubble_low_confidence_flagged() -> None:
    assert Bubble.model_validate_json(bubble(0.89)).needs_review
    assert not Bubble.model_validate_json(bubble(0.90)).needs_review


def test_bubble_speaker_must_cite_own_panel() -> None:
    with pytest.raises(ValidationError):
        Bubble.model_validate_json(bubble(0.95, {"value": "char_ada", "panel_ids": ["panel_9"]}))


def test_bubble_reading_order_required_and_positive() -> None:
    raw = json.loads(bubble(0.95))
    for bad in (0, -1, None):
        with pytest.raises(ValidationError, match="reading_order"):
            Bubble.model_validate_json(j({**raw, "reading_order": bad}))
    del raw["reading_order"]
    with pytest.raises(ValidationError, match="reading_order"):
        Bubble.model_validate_json(j(raw))


# --- Page ----------------------------------------------------------------------------------------


def page(**over: Any) -> str:
    base: dict[str, Any] = {
        "id": "page_1",
        "provenance": PROV,
        "chapter_id": "chap_1",
        "index": 1,
        "source_page": 3,
        "width": 100,
        "height": 200,
        "image_ref": REF,
    }
    base.update(over)
    return j(base)


def test_page_source_position() -> None:
    p = Page.model_validate_json(page())
    assert (p.index, p.source_page, p.source_y0, p.source_y1) == (1, 3, None, None)
    sliced = Page.model_validate_json(page(source_y0=0, source_y1=200))
    assert (sliced.source_y0, sliced.source_y1) == (0, 200)
    for bad in (
        {"source_page": 0},
        {"index": 0},
        {"source_y0": 0},  # both or neither
        {"source_y1": 10},
        {"source_y0": 10, "source_y1": 10},  # y1 > y0
        {"source_y0": -1, "source_y1": 10},
    ):
        with pytest.raises(ValidationError):
            Page.model_validate_json(page(**bad))
    raw = json.loads(page())
    del raw["source_page"]
    with pytest.raises(ValidationError, match="source_page"):
        Page.model_validate_json(j(raw))


# --- Provenance components -----------------------------------------------------------------------


def test_provenance_components() -> None:
    assert Provenance.model_validate_json(j(PROV)).components == []
    ref = {
        "name": "manga-ocr",
        "revision": "a" * 40,
        "license": "CC-BY-SA-4.0",
        "source_url": "https://huggingface.co/x",
    }
    prov = Provenance.model_validate_json(j({**PROV, "components": [ref]}))
    assert prov.components == [ModelRef(**ref)]
    for bad in ({"source_url": "http://x"}, {"extra": 1}, {"revision": "r" * 81}):
        with pytest.raises(ValidationError):
            Provenance.model_validate_json(j({**PROV, "components": [{**ref, **bad}]}))


def test_ocr_threshold_matches_config() -> None:
    cfg = yaml.safe_load(Path("configs/thresholds.yaml").read_text())
    assert cfg["ocr_confidence_min"] == OCR_CONFIDENCE_MIN


# --- Shot ----------------------------------------------------------------------------------------


def test_shot_requires_source_panel() -> None:
    with pytest.raises(ValidationError):
        Shot.model_validate_json(
            j(
                {
                    "id": "shot_1",
                    "provenance": PROV,
                    "source_panel_ids": [],
                    "camera_move": "pan",
                    "duration_ms": 2000,
                }
            )
        )


# --- Rights --------------------------------------------------------------------------------------


def rights(**over: Any) -> RightsRecord:
    base: dict[str, Any] = {
        "id": "rights_1",
        "project_id": "proj_1",
        "basis": "public_domain",
        "justification": "Published 1941, not renewed",
        "created_by": "user_a",
    }
    base.update(over)
    return RightsRecord.model_validate_json(j(base))


def approved(**over: Any) -> RightsRecord:
    return rights(
        status="approved",
        evidence_doc_ref=REF,
        approved_by="user_b",
        approved_at=NOW.isoformat(),
        **over,
    )


def test_rights_gate_blocks_missing_pending_expired() -> None:
    with pytest.raises(RightsDenied):
        require_render_allowed(None, NOW)
    with pytest.raises(RightsDenied, match="pending"):
        require_render_allowed(rights(), NOW)
    expired = approved(expires_at=(NOW - timedelta(days=1)).isoformat())
    with pytest.raises(RightsDenied, match="expired"):
        require_render_allowed(expired, NOW)
    assert require_render_allowed(approved(), NOW).status == "approved"


def test_rights_cloud_gate() -> None:
    with pytest.raises(RightsDenied, match="hosted provider"):
        require_cloud_allowed(approved(), NOW)
    assert require_cloud_allowed(approved(allows_cloud_processing=True), NOW)


def test_rights_approval_needs_evidence_and_second_person() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        rights(status="approved", approved_by="user_b", approved_at=NOW.isoformat())
    with pytest.raises(ValidationError, match="creator"):
        rights(
            status="approved",
            evidence_doc_ref=REF,
            approved_by="user_a",
            approved_at=NOW.isoformat(),
        )
    with pytest.raises(ValidationError, match="licensor"):
        rights(basis="licensed")


# --- Voices --------------------------------------------------------------------------------------


def voice(**over: Any) -> Voice:
    base: dict[str, Any] = {
        "id": "v_actor1",
        "name": "[ACTOR NAME]",
        "description": "mezzo",
        "source": "licensed_actor",
        "consent_status": "pending",
        "engine": "cosyvoice2",
        "languages": ["ja", "en"],
    }
    base.update(over)
    return Voice.model_validate_json(j(base))


def test_voice_consent_gate() -> None:
    with pytest.raises(VoiceLocked):
        require_voice_usable(voice())
    with pytest.raises(VoiceLocked):
        require_voice_usable(voice(consent_status="revoked"))
    assert require_voice_usable(voice(consent_status="signed", consent_doc_ref=REF)).usable
    designed = voice(id="v_granite", source="designed", consent_status="not_required")
    assert designed.usable


def test_voice_consent_rules() -> None:
    with pytest.raises(ValidationError, match="consent document"):
        voice(consent_status="signed")
    with pytest.raises(ValidationError, match="always needs consent"):
        voice(consent_status="not_required")
    with pytest.raises(ValidationError, match="reference audio"):
        voice(source="cloned")


def test_voice_line_audio_must_be_watermarked() -> None:
    base = {
        "id": "line_1",
        "provenance": PROV,
        "bubble_id": "bubble_1",
        "panel_id": "panel_1",
        "character_id": "char_ada",
        "language": "en",
        "text": "Hi",
        "emotion": {},
        "voice_id": "v_ember",
        "casting_version": 1,
        "audio_ref": REF,
    }
    with pytest.raises(ValidationError, match="watermarked"):
        VoiceLine.model_validate_json(j(base))
    assert VoiceLine.model_validate_json(j({**base, "watermarked": True})).watermarked


# --- Validation report ---------------------------------------------------------------------------


def test_report_verdict_and_missing_value_never_passes() -> None:
    ok = Check(name="whisper_wer", metric="WER", value=0.05, threshold=0.15, higher_is_better=False)
    missing = Check(name="identity", metric="cos", value=None, threshold=0.9, higher_is_better=True)
    rep = ValidationReport.model_validate_json(
        j(
            {
                "id": "report_1",
                "provenance": PROV,
                "artifact_id": "shot_1",
                "checks": [
                    ok.model_dump(exclude={"passed"}),
                    missing.model_dump(exclude={"passed"}),
                ],
            }
        )
    )
    assert ok.passed and not missing.passed and rep.verdict == "review"
