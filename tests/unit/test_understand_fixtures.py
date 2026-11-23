from __future__ import annotations

import json
from pathlib import Path

from pipeline.providers.replay import ReplayProvider
from pipeline.understand.contract import PanelReading

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "understand"


def test_recorded_outputs_validate_against_the_current_contract() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert {f.stem for f in files} == {"speech", "blank", "night"}  # injection masks to speech
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["outputs"]
        for raw in data["outputs"]:
            PanelReading.model_validate_json(raw)


def test_replay_provider_loads_the_fixture_directory() -> None:
    ReplayProvider(FIXTURES)  # no duplicate keys: masked injection and clean are one fixture
