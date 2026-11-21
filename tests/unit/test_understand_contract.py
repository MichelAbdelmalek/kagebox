from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from pipeline.common.schemas import Provenance
from pipeline.providers.registry import ModelNotApproved, model_for_stage
from pipeline.understand.contract import (
    REVIEW_KINDS,
    Claim,
    PanelReading,
    normalise,
    schema_sha256,
    to_scene,
)

PANEL = "panel_0144"
PROV = Provenance(
    stage="understand",
    tool="test-vlm",
    tool_version="0",
    license="Apache-2.0",
    provider="local",
    params_hash="0" * 64,
    created_at=datetime(2026, 11, 21, tzinfo=UTC),
)
NULL: dict[str, Any] = {"value": None, "evidence": None, "quote": None}


def visual(value: str) -> dict[str, Any]:
    return {"value": value, "evidence": "visual", "quote": None}


def text(value: str, quote: str) -> dict[str, Any]:
    return {"value": value, "evidence": "text", "quote": quote}


def reading(**over: Any) -> PanelReading:
    base: dict[str, Any] = {
        "setting": NULL,
        "action": NULL,
        "time_of_day": NULL,
        "weather": NULL,
        "shot_hint": None,
        "figures": [],
    }
    base.update(over)
    return PanelReading.model_validate(base)


def convert(r: PanelReading, ocr: str = "") -> Any:
    return to_scene(r, scene_id="scene_1", panel_id=PANEL, ocr_text=ocr, provenance=PROV)


def test_visual_claim_is_cited_to_own_panel() -> None:
    scene, issues = convert(reading(setting=visual("Clock chamber"), shot_hint="wide"))
    assert issues == []
    assert scene.setting.value == "Clock chamber"
    assert scene.setting.panel_ids == [PANEL]
    assert scene.shot_hint.value == "wide" and scene.shot_hint.panel_ids == [PANEL]
    assert scene.panel_id == PANEL and scene.id == "scene_1"


def test_text_claim_with_exact_quote_kept() -> None:
    ocr = "It was raining\uff0c Holt\uff01  Run\u3000now."
    scene, issues = convert(
        reading(weather=text("raining", "was raining, Holt!"), action=text("run", "Run now")),
        ocr,
    )
    assert scene.weather.value == "raining" and scene.weather.panel_ids == [PANEL]
    assert scene.action.value == "run" and issues == []


@pytest.mark.parametrize(
    "quote",
    ["It was rainxng", "The tower falls at dawn", "I"],
    ids=["one_char_off", "other_panel_text", "one_char_quote"],
)
def test_text_claim_with_missing_or_near_miss_quote_is_nulled_with_issue(quote: str) -> None:
    scene, issues = convert(reading(weather=text("raining", quote)), "It was raining, Holt!")
    assert scene.weather.value is None and scene.weather.panel_ids == []
    assert [(i.kind, i.field) for i in issues] == [("scene_unsupported_quote", "weather")]


def test_text_claim_on_panel_without_ocr_text_is_nulled_with_issue() -> None:
    scene, issues = convert(reading(time_of_day=text("night", "midnight")), "")
    assert scene.time_of_day.value is None
    assert [(i.kind, i.field) for i in issues] == [("scene_unsupported_quote", "time_of_day")]


def test_all_null_reading_is_valid_and_issue_free() -> None:
    scene, issues = convert(reading(), "some words")
    assert issues == []
    assert scene.unknown_fields == ["setting", "action", "shot_hint", "time_of_day", "weather"]
    assert scene.figures == []
    assert scene.characters_present == [] and scene.emotion_by_character == {}


def _keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in _keys(v)]
    if isinstance(node, list):
        return [k for v in node for k in _keys(v)]
    return []


def test_reading_schema_has_no_ids_or_panel_fields() -> None:
    for key in _keys(PanelReading.model_json_schema()):
        low = key.lower()
        assert low != "id" and not low.endswith(("_id", "_ids")), key
        assert "panel" not in low and "cite" not in low, key
    assert len(schema_sha256()) == 64 and schema_sha256() == schema_sha256()


def test_figures_cite_own_panel_and_figure_without_description_dropped() -> None:
    figures = [
        {"description": visual("A knight kneeling"), "emotion": visual("grief")},
        {"description": NULL, "emotion": visual("anger")},
        {"description": visual("A girl"), "emotion": NULL},
        {"description": text("A boy", "nobody said this"), "emotion": text("calm", "Holt")},
    ]
    scene, issues = convert(reading(figures=figures), "Holt")
    assert [f.description.value for f in scene.figures] == ["A knight kneeling", "A girl"]
    assert all(f.description.panel_ids == [PANEL] for f in scene.figures)
    assert scene.figures[0].emotion.panel_ids == [PANEL]
    assert scene.figures[1].emotion.value is None
    assert [i.field for i in issues] == ["figures[3].description"]


def test_claim_value_without_evidence_or_text_without_quote_rejected() -> None:
    with pytest.raises(ValidationError):
        Claim.model_validate({"value": "x", "evidence": None, "quote": None})
    with pytest.raises(ValidationError):
        Claim.model_validate({"value": "x", "evidence": "text", "quote": None})
    with pytest.raises(ValidationError):
        Claim.model_validate({"value": None, "evidence": "visual", "quote": None})
    with pytest.raises(ValidationError):
        Claim.model_validate({"value": None, "evidence": None, "quote": "stray"})


def test_normalise_unifies_width_punctuation_and_spacing() -> None:
    wide = "\uff28\uff4f\uff4c\uff54\uff0c\u3000\uff52\uff55\uff4e\uff01"
    assert normalise(wide) == normalise("Holt, run!")
    assert normalise("a b\n c") == "abc"
    assert normalise("\u300c待て\u3002\u300d") == normalise('"待て."')


def test_review_kinds() -> None:
    assert REVIEW_KINDS == ("scene_failed", "scene_unsupported_quote")


def test_model_for_stage_understand_not_approved_yet() -> None:
    with pytest.raises(ModelNotApproved):
        model_for_stage("understand")
