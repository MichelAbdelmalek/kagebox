from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.providers.replay import ReplayProvider
from pipeline.understand.contract import PanelReading
from pipeline.understand.settings import load_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "understand"


def check_recorded_outputs(name: str, outputs: list[str]) -> None:
    """The last output is the valid answer; every earlier one is a failed attempt, so it must
    still be invalid (a cut-off or malformed answer). A valid earlier output would be a fixture
    that never needed its retry, or a contract that drifted under it."""
    assert outputs, name
    PanelReading.model_validate_json(outputs[-1])  # drift in the contract fails here
    for earlier in outputs[:-1]:
        with pytest.raises(ValidationError):  # truncated JSON is a ValidationError too
            PanelReading.model_validate_json(earlier)


def test_recorded_outputs_validate_against_the_current_contract() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert {f.stem for f in files} == {"speech", "blank", "night"}  # injection masks to speech
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        # a cut-off first attempt is recorded as it happened; the last output is the valid retry
        check_recorded_outputs(f.stem, data["outputs"])


def test_check_recorded_outputs_refuses_a_valid_earlier_output() -> None:
    good = json.dumps({"setting": {}, "action": {}, "time_of_day": {}, "weather": {}})
    check_recorded_outputs("ok", ['{"setting": {"value": "a', good])  # truncated, then valid
    with pytest.raises(AssertionError):  # nothing recorded
        check_recorded_outputs("empty", [])
    with pytest.raises(pytest.fail.Exception):  # a valid first attempt followed by another
        check_recorded_outputs("two-good", [good, good])


def test_fixtures_record_the_settings_they_were_made_with() -> None:
    """Re-recording is a deliberate act: a change of mask margin, mask threshold or prompt
    version without new fixtures fails here (the prompt hash is pinned in the replay tests)."""
    s = load_settings()
    expected = {
        "margin_px": s.margin_px,
        "mask_detect_threshold": s.mask_detect_threshold,
        "prompt_version": s.prompt_version,
    }
    for f in sorted(FIXTURES.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data.get("recorded_with") == expected, f.name


def test_replay_provider_loads_the_fixture_directory() -> None:
    ReplayProvider(FIXTURES)  # no duplicate keys: masked injection and clean are one fixture
