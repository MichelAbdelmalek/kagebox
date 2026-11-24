"""The offline understand report: pure aggregation and a build over a stored (fake) run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from orchestration.understand_report import (
    ascii_line,
    build_report,
    figures_summary,
    null_rates,
    pace_stats,
    panel_seconds,
    percentile,
)
from pipeline.common.schemas import SceneJSON
from pipeline.providers.base import StructuredOutputFailed
from pipeline.providers.registry import model_for_stage
from pipeline.understand.contract import Claim, FigureReading
from tests.unit.understand_env import make_env, reading

SECRET = "SENTINEL-SCENE-77aa"


def _scene(**over: object) -> SceneJSON:
    from datetime import UTC, datetime

    pid = "panel_x"
    cited = {"value": None, "panel_ids": []}
    on = lambda v: {"value": v, "panel_ids": [pid]}  # noqa: E731
    base = {
        "id": "scene_x",
        "panel_id": pid,
        "setting": on("a room"),
        "action": cited,
        "shot_hint": on("medium"),
        "time_of_day": cited,
        "weather": cited,
        "figures": [],
        "provenance": {
            "stage": "understand",
            "tool": "t",
            "tool_version": "1",
            "license": "MIT",
            "provider": "local",
            "params_hash": "a" * 64,
            "created_at": datetime.now(UTC),
        },
    }
    base.update(over)
    return SceneJSON.model_validate(base)


def test_null_rates_count_unknown_fields() -> None:
    scenes = [_scene(), _scene(setting={"value": None, "panel_ids": []})]
    rates = null_rates(scenes)
    assert rates["setting"] == {"null": 1, "total": 2, "null_pct": 50.0}
    assert rates["action"] == {"null": 2, "total": 2, "null_pct": 100.0}
    assert rates["shot_hint"]["null"] == 0
    assert set(rates) == {"setting", "action", "shot_hint", "time_of_day", "weather"}
    assert null_rates([])["setting"] == {"null": 0, "total": 0, "null_pct": 0.0}


def test_figures_summary_histogram_and_empty_descriptions() -> None:
    one = {
        "description": {"value": "a boy", "panel_ids": ["panel_x"]},
        "emotion": {"value": None, "panel_ids": []},
    }
    s = figures_summary([_scene(), _scene(figures=[one]), _scene(figures=[one, one])])
    assert s["per_panel"] == [0, 1, 2]
    assert s["histogram"] == {"0": 1, "1": 1, "2": 1}
    assert s["mean"] == 1.0 and s["emotion_null_pct"] == 100.0
    assert figures_summary([]) == {
        "per_panel": [],
        "histogram": {},
        "mean": None,
        "emotion_null_pct": None,
    }


def test_pace_reports_n_and_calls_p95_the_max_below_twenty_panels() -> None:
    few = pace_stats([float(i) for i in range(1, 11)])  # n = 10
    assert few["n"] == 10 and few["p95_is_max"] is True
    assert few["p95_s"] == few["max_s"] == 10.0  # nearest-rank p95 of 10 values IS the max
    edge = pace_stats([float(i) for i in range(1, 20)])  # n = 19
    assert edge["p95_is_max"] is True
    enough = pace_stats([float(i) for i in range(1, 21)])  # n = 20
    assert enough["n"] == 20 and enough["p95_is_max"] is False and enough["p95_s"] == 19.0
    none = pace_stats([])
    assert none["n"] == 0 and none["p95_s"] is None and none["max_s"] is None


def test_percentile_nearest_rank_and_panel_seconds_from_timestamps() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0
    assert percentile([], 50) is None
    # first panel is measured from the stage start minus the model load; later ones are deltas
    secs = panel_seconds([110.0, 125.0, 133.0], started=0.0, load_s=100.0)
    assert secs == [10.0, 15.0, 8.0]
    assert panel_seconds([], 0.0, 0.0) == []


def test_ascii_line_never_draws_text_the_default_font_cannot() -> None:
    assert ascii_line("a quiet room") == "a quiet room"
    assert ascii_line("天気は嵐") == "[non-latin text: see report.json]"
    assert ascii_line(None) == "null"
    assert len(ascii_line("x" * 500, 40)) == 40


def test_report_counts_null_rates_and_review_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=4)

    def on_call(n: int):  # type: ignore[no-untyped-def]
        if n == 3:
            raise StructuredOutputFailed(model_for_stage("understand"), 2, "ValidationError")
        if n == 2:
            return reading(weather=Claim(value=SECRET, evidence="text"))
        if n == 4:
            fig = FigureReading(
                description=Claim(value="a seated man", evidence="visual"), emotion=Claim()
            )
            return reading(figures=[fig, fig])
        return None

    env.provider.on_call = on_call
    env.run(run_id="run-r")  # full run: 3 scenes, 1 failure
    out = tmp_path / "rep"
    report = build_report(env.engine, env.store, env.chapter_id, out)

    assert report["panels"] == 4 and report["scenes"] == 3 and report["failed"] == 1
    assert report["review_items_by_kind"] == {"scene_failed": 1, "scene_text_claim": 1}
    assert report["null_rates"]["weather"]["null"] == 3  # the text claim was dropped to null
    assert report["null_rates"]["shot_hint"]["null"] == 0
    assert report["figures"]["per_panel"] == [0, 0, 2]  # scenes in reading order, panel 3 absent
    assert report["stage"]["status"] == "done" and report["stage"]["trial"] is False
    assert report["wall_time_s"] is not None and report["wall_time_s"] >= 0
    assert len(report["panels_detail"]) == 4
    assert [p["scene"] is None for p in report["panels_detail"]] == [False, False, True, False]
    # the model text is data in report.json only; the sheet is a picture
    assert (out / "report.json").is_file()
    assert json.loads((out / "report.json").read_text("utf-8"))["scenes"] == 3
    sheets = report["sheets"]
    assert sheets and all((out / s).is_file() for s in sheets)
    with Image.open(out / sheets[0]) as im:
        assert im.width > 100 and im.height > 100


def test_trial_report_carries_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=4)
    env.run(limit_panels=2, run_id="run-t")
    report = build_report(env.engine, env.store, env.chapter_id, tmp_path / "rep")
    st = report["stage"]
    assert st["trial"] is True and st["status"] == "pending"
    assert st["projected_total_s"] is not None and st["uncached_remaining"] == 2
    assert report["panels"] == 4 and report["scenes"] == 2
    assert report["seconds_per_panel"]["measured_panels"] == 2


def test_pick_panels_trial_first_then_forced_then_evenly_spaced() -> None:
    from orchestration.understand_report import pick_panels

    ids = [f"p{i}" for i in range(40)]
    got = pick_panels(ids, trial=10, extra=5, forced=["p20", "p3"])
    assert got[:10] == ids[:10]  # the trial panels
    assert got[10] == "p20" and "p3" not in got[10:]  # forced, no duplicates
    assert len(got) == len(set(got)) == 10 + 1 + 5
    assert pick_panels(ids[:3], trial=10, extra=5) == ids[:3]
