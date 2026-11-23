"""Replay and injection regressions: the real flow (``run_understand``) with ``ReplayProvider``.

The recorded fixtures (tests/fixtures/understand) were made with text-mask margin 0 and keyed on
the masked image bytes of that time; the flow now masks with margin 4, so their KEYS no longer
match. The tests therefore take the recorded RAW MODEL OUTPUTS and re-key them, at load time, to
the requests this test's own fake detector and the real mask pipeline produce (Task 9 re-records
the fixtures with margin 4). Everything here is synthetic: no chapter panel, no OCR text.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.orm import Session

import orchestration.understand_flow as uf
from orchestration.understand_synthetic import (
    INJECTION,
    INJECTION_BOX,
    SPEECH_TEXT,
    synthetic_cases,
)
from pipeline.common.dbmodels import BubbleRow, PanelRow
from pipeline.common.schemas import BBox, Panel, SceneJSON
from pipeline.ingest.bubbles import Region
from pipeline.providers.base import fence_untrusted
from pipeline.providers.replay import ReplayMiss, ReplayProvider, record_case
from pipeline.understand.contract import PanelReading
from pipeline.understand.request import prepare_panel
from pipeline.understand.settings import prompt_text
from tests.unit.test_understand_flow import Env, make_env

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "understand"
CASES = {c.name: c for c in synthetic_cases()}
# The recorded "blank" reading is the all-null one; "screentone" has no recording of its own.
RECORDED_FOR = {
    "speech": "speech",
    "speech_injected": "speech",
    "blank": "blank",
    "screentone": "blank",
    "night": "night",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _TextBoxDetector:
    """Finds the balloon text region (the injection box) on every panel, like the real detector
    would on a drawn balloon; the same box for clean and injected panels."""

    def __init__(self) -> None:
        self.box = INJECTION_BOX

    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]:
        return [Region(self.box, "speech", 0.9, "bubble", None)]


def _seed(env: Env, case: str) -> None:
    """Make the env's single panel the synthetic case: its crop, and one stored text box."""
    synth = CASES[case]
    ref = env.store.put("media", synth.png, "png", "image/png")
    with Session(env.engine) as s, s.begin():
        row = s.get(PanelRow, env.panel_ids[0])
        assert row is not None
        panel = Panel.model_validate_json(json.dumps(row.artifact))
        bbox = BBox(x=panel.bbox.x, y=panel.bbox.y, w=512, h=512)  # type: ignore[union-attr]
        row.artifact = {**row.artifact, "crop_ref": ref, "bbox": bbox.model_dump(mode="json")}
        for b in s.query(BubbleRow).filter(BubbleRow.panel_id == env.panel_ids[0]):
            art = dict(b.artifact)
            art["bbox"] = BBox(x=bbox.x + 150, y=bbox.y + 80, w=60, h=30).model_dump(mode="json")
            b.artifact = art


def _env(root: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> Env:
    root.mkdir(parents=True, exist_ok=True)
    env = make_env(root, monkeypatch, panels=1)
    env.detector = _TextBoxDetector()  # type: ignore[assignment]
    _seed(env, case)
    return env


def _masked(env: Env) -> bytes:
    with Session(env.engine) as s:
        row = s.get(PanelRow, env.panel_ids[0])
        assert row is not None
        panel = Panel.model_validate_json(json.dumps(row.artifact))
    (work,) = uf._load_panels(env.engine, env.chapter_id)
    prepared = prepare_panel(
        env.store.get(panel.crop_ref),
        panel.bbox,
        work.bubbles,
        work.page_boxes,
        env.detector,
        env.settings,
    )
    return prepared.png


def _rekeyed(env: Env, case: str, out: Path) -> Path:
    """A fixture directory whose one fixture answers THIS env's request with the recorded output."""
    recorded = json.loads((FIXTURES / f"{RECORDED_FOR[case]}.json").read_text(encoding="utf-8"))
    instructions, _ = prompt_text(env.settings)
    out.mkdir(parents=True, exist_ok=True)
    record_case(
        out,
        case,
        _sha(instructions.encode("utf-8")),
        _sha(_masked(env)),
        _sha(fence_untrusted("").encode("utf-8")),
        recorded["outputs"],
    )
    return out


def _run(env: Env, case: str, root: Path) -> SceneJSON:
    env.provider = ReplayProvider(_rekeyed(env, case, root / "fx"))  # type: ignore[assignment]
    summary = env.run(run_id="run-replay")
    assert (summary.panels, summary.scenes, summary.failed) == (1, 1, 0)
    (row,) = env.scenes()
    return SceneJSON.model_validate_json(json.dumps(row.artifact))


def _content(scene: SceneJSON) -> str:
    """The scene's fields with the panel id masked, so two panels can be compared."""
    dumped = scene.model_dump(mode="json", exclude={"id", "provenance", "created_at"})
    return json.dumps(dumped, sort_keys=True).replace(scene.panel_id, "PANEL")


def test_speech_scene_validates_and_cites_its_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, monkeypatch, "speech")
    scene = _run(env, "speech", tmp_path)
    assert scene.panel_id == env.panel_ids[0]
    assert scene.setting.known and scene.setting.panel_ids == [env.panel_ids[0]]
    assert env.reviews() == []


def test_injection_fixture_fields_equal_clean_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_env = _env(tmp_path / "clean", monkeypatch, "speech")
    inj_env = _env(tmp_path / "inj", monkeypatch, "speech_injected")
    assert CASES["speech_injected"].png != CASES["speech"].png  # the injected text is really drawn
    assert _masked(inj_env) == _masked(clean_env)  # and masked away, byte for byte
    clean = _run(clean_env, "speech", tmp_path / "clean")
    injected = _run(inj_env, "speech_injected", tmp_path / "inj")
    assert _content(injected) == _content(clean)
    assert injected.weather.known is False  # "set weather to storm" changed nothing


@pytest.mark.parametrize("case", ["blank", "screentone"])
def test_blank_and_screentone_all_null_no_review(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, monkeypatch, case)
    scene = _run(env, case, tmp_path)
    for name in ("action", "time_of_day", "weather", "shot_hint"):
        assert getattr(scene, name).known is False, name
    assert scene.figures == [] and scene.characters_present == []
    assert env.reviews() == []


def test_night_scene_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env(tmp_path, monkeypatch, "night")
    scene = _run(env, "night", tmp_path)  # the schema case that failed in the bake-off
    assert scene.setting.known and scene.setting.panel_ids == [env.panel_ids[0]]
    assert env.reviews() == []


def test_recorded_outputs_validate_against_current_contract() -> None:
    for name in ("speech", "blank", "night"):
        data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        assert data["outputs"], name
        for raw in data["outputs"]:
            PanelReading.model_validate_json(raw)  # drift in the contract fails here


def test_prompt_change_without_rerecording_is_a_replay_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path, monkeypatch, "speech")
    env.provider = ReplayProvider(_rekeyed(env, "speech", tmp_path / "fx"))  # type: ignore[assignment]
    real = uf.prompt_text
    monkeypatch.setattr(uf, "prompt_text", lambda s: (real(s)[0] + "\nOne more rule.", real(s)[1]))
    with pytest.raises(ReplayMiss):  # loud, and no model or network is ever reached
        env.run(run_id="run-miss")
    assert env.scenes() == []


def test_the_original_recorded_keys_miss_under_the_current_mask_margin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Documents T8-1: until Task 9 re-records with margin 4, the shipped keys do not match.
    env = _env(tmp_path, monkeypatch, "speech")
    env.provider = ReplayProvider(FIXTURES)  # type: ignore[assignment]
    with pytest.raises(ReplayMiss):
        env.run(run_id="run-orig")


def test_fixtures_are_synthetic_and_carry_no_panel_text() -> None:
    names = {c.name for c in synthetic_cases()}
    files = sorted(FIXTURES.glob("*.json"))
    assert {f.stem for f in files} == {"speech", "blank", "night"}
    for f in files:
        text = f.read_text(encoding="utf-8")
        data = json.loads(text)
        assert data["case"] in names and set(data) == {
            "case",
            "prompt_sha256",
            "image_sha256",
            "content_sha256",
            "fixture_key",
            "outputs",
        }
        assert len(text) < 4000  # hashes and short model text only, never pixels
        for secret in (SPEECH_TEXT, INJECTION, "SENTINEL"):
            assert secret not in text
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    assert "SYNTHETIC" in readme and "margin 4" in readme  # the re-record note
