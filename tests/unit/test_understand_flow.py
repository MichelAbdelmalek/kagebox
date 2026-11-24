"""The chapter-understand flow: SQLite, in-memory object store, fake detector and provider."""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from prefect.runtime import flow_run as prefect_flow_run
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

import orchestration.understand_flow as uf
from orchestration.understand_flow import (
    BAD_TRIAL,
    BUDGET,
    MAX_TRIAL_PANELS,
    UnderstandAlreadyRunning,
    UnderstandDeps,
    UnderstandRefused,
    project_total_s,
    run_understand,
    understand_flow,
)
from pipeline.common.database import make_engine
from pipeline.common.dbmodels import (
    STALE_INGEST_S,
    Base,
    BubbleRow,
    ChapterRow,
    JobRow,
    PageRow,
    PanelRow,
    ProjectRow,
    ReviewItemRow,
    RightsRow,
    SceneRow,
    StageRunRow,
    User,
)
from pipeline.common.ids import new_id
from pipeline.common.schemas import (
    BBox,
    Bubble,
    Page,
    Panel,
    Provenance,
    RightsDenied,
    RightsRecord,
    SceneJSON,
)
from pipeline.common.settings import Settings
from pipeline.common.storage import MemoryObjectStore
from pipeline.ingest.bubbles import Region
from pipeline.providers.base import ModelLoadFailed, ModelSpec, StructuredOutputFailed
from pipeline.providers.registry import model_for_stage
from pipeline.understand.contract import Claim, FigureReading, PanelReading
from pipeline.understand.settings import load_settings

EVIDENCE = "evidence/sha256/" + "a" * 64 + ".pdf"
SENTINEL = "SENTINEL-OCR-31ce"
OUT_SENTINEL = "SENTINEL-OUT-8d02"
CROP = (60, 40)


@pytest.fixture(scope="module", autouse=True)
def harness() -> Iterator[None]:
    with prefect_test_harness(server_startup_timeout=120):
        yield


# ---------------------------------------------------------------------------------------------
# fakes


class Interrupted(BaseException):
    """Stands in for Prefect's TerminationSignal (a BaseException, not an Exception)."""


class FakeDetector:
    def __init__(self) -> None:
        self.calls = 0
        self.exc: Exception | None = None
        self.region = Region(BBox(x=10, y=10, w=20, h=20), "speech", 0.9, "bubble", None)

    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return [self.region]


def reading(**over: Any) -> PanelReading:
    base: dict[str, Any] = {
        "setting": Claim(value="a quiet room", evidence="visual"),
        "action": Claim(value="a figure stands", evidence="visual"),
        "time_of_day": Claim(),
        "weather": Claim(),
        "shot_hint": "medium",
        "figures": [],
    }
    base.update(over)
    return PanelReading(**base)


@dataclass
class FakeProvider:
    """Records every call; ``on_call(n)`` runs first and may raise or return a reading."""

    on_call: Callable[[int], PanelReading | None] | None = None
    calls: list[tuple[str, str, list[bytes]]] = field(default_factory=list)
    loads: int = 0
    on_load: Callable[[], None] | None = None

    def load(self, spec: ModelSpec) -> None:
        self.loads += 1
        if self.on_load is not None:
            self.on_load()

    def vlm_structured(
        self, spec: ModelSpec, instructions: str, content: str, images: list[bytes], schema: Any
    ) -> Any:
        self.calls.append((instructions, content, images))
        if self.on_call is not None:
            out = self.on_call(len(self.calls))
            if out is not None:
                return out
        return reading()

    def llm_structured(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("not used")


@dataclass
class Env:
    engine: Engine
    store: MemoryObjectStore
    project_id: str
    chapter_id: str
    panel_ids: list[str]
    detector: FakeDetector
    provider: FakeProvider
    factory_calls: list[ModelSpec] = field(default_factory=list)
    settings: Any = None
    factory_exc: Exception | None = None

    def deps(self) -> UnderstandDeps:
        def factory(spec: ModelSpec) -> FakeProvider:
            self.factory_calls.append(spec)
            if self.factory_exc is not None:
                raise self.factory_exc
            return self.provider

        return UnderstandDeps(
            engine=self.engine,
            store=self.store,
            settings=self.settings,
            provider_factory=factory,  # type: ignore[arg-type]
            detector_factory=lambda: self.detector,
        )

    def run(self, **kw: Any) -> uf.UnderstandSummary:
        return run_understand(self.chapter_id, self.deps(), **kw)

    def stage(self) -> StageRunRow:
        with Session(self.engine) as s:
            row = s.get(StageRunRow, (self.chapter_id, "understand"))
            assert row is not None
            return row

    def scenes(self) -> list[SceneRow]:
        with Session(self.engine) as s:
            return list(s.scalars(select(SceneRow).where(SceneRow.chapter_id == self.chapter_id)))

    def reviews(self, kind: str | None = None) -> list[ReviewItemRow]:
        with Session(self.engine) as s:
            q = select(ReviewItemRow).where(ReviewItemRow.chapter_id == self.chapter_id)
            if kind:
                q = q.where(ReviewItemRow.kind == kind)
            return list(s.scalars(q))

    def panel(self, panel_id: str) -> Panel:
        with Session(self.engine) as s:
            row = s.get(PanelRow, panel_id)
            assert row is not None
            return Panel.model_validate_json(json.dumps(row.artifact))

    def update(self, model: Any, key: Any, **values: Any) -> None:
        with Session(self.engine) as s, s.begin():
            row = s.get(model, key)
            assert row is not None
            for k, v in values.items():
                setattr(row, k, v)


def prov(stage: str = "ingest") -> Provenance:
    return Provenance(
        stage=stage,  # type: ignore[arg-type]
        tool="t",
        tool_version="1",
        license="MIT",
        provider="none",
        params_hash="a" * 64,
        created_at=datetime.now(UTC),
    )


def rights(project_id: str, **over: Any) -> RightsRecord:
    base: dict[str, Any] = {
        "id": new_id("rights"),
        "project_id": project_id,
        "basis": "public_domain",
        "justification": "PD",
        "created_by": "user_a",
        "status": "approved",
        "evidence_doc_ref": EVIDENCE,
        "approved_by": "user_b",
        "approved_at": datetime.now(UTC),
    }
    base.update(over)
    return RightsRecord(**base)


def crop_png(seed: int) -> bytes:
    im = Image.new("RGB", CROP, (200, 210, 220))
    for x in range(CROP[0]):
        for y in range(10, 30):
            im.putpixel((x, y), (0, 0, 0) if (x + y + seed) % 2 else (255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def make_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, panels: int = 4, **rec: Any) -> Env:
    engine = make_engine(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))
    Base.metadata.create_all(engine)
    store = MemoryObjectStore()
    user_id, project_id, chapter_id = new_id("user"), new_id("proj"), new_id("chap")
    record = rights(project_id, **rec)
    page_id = new_id("page")
    panel_ids = [new_id("panel") for _ in range(panels)]
    with Session(engine) as s, s.begin():
        s.add(User(id=user_id, email="a@b.c", password_hash="x"))
        s.flush()
        s.add(ProjectRow(id=project_id, title="T", format="manga_rtl", created_by=user_id))
        s.flush()
        s.add(
            RightsRow(
                id=record.id,
                project_id=project_id,
                status=record.status,
                record=record.model_dump(mode="json"),
            )
        )
        s.add(
            ChapterRow(
                id=chapter_id,
                project_id=project_id,
                number=1,
                language="ja",
                format="manga_rtl",
                source_kind="import",
                source_ref="x.cbz",
                source_sha256="b" * 64,
                page_start=1,
                page_end=1,
                status="ready",
            )
        )
        s.flush()
        image_ref = store.put("media", b"page", "png", "image/png")
        page = Page(
            id=page_id,
            chapter_id=chapter_id,
            index=1,
            source_page=1,
            width=600,
            height=800,
            image_ref=image_ref,
            provenance=prov(),
        )
        s.add(
            PageRow(
                id=page_id, chapter_id=chapter_id, index=1, artifact=page.model_dump(mode="json")
            )
        )
        s.flush()
        for n, pid in enumerate(panel_ids, 1):
            ref = store.put("media", crop_png(n), "png", "image/png")
            panel = Panel(
                id=pid,
                page_id=page_id,
                bbox=BBox(x=100, y=100 * n, w=CROP[0], h=CROP[1]),
                reading_order=n,
                crop_ref=ref,
                provenance=prov(),
            )
            s.add(
                PanelRow(
                    id=pid,
                    page_id=page_id,
                    chapter_id=chapter_id,
                    reading_order=n,
                    artifact=panel.model_dump(mode="json"),
                )
            )
        s.flush()
        bubble = Bubble(
            id=new_id("bubble"),
            panel_id=panel_ids[0],
            bbox=BBox(x=110, y=110, w=10, h=10),
            kind="speech",
            text=SENTINEL,
            lang="ja",
            ocr_confidence=0.95,
            ocr_engine="manga-ocr",
            speaker={"value": None, "panel_ids": []},  # type: ignore[arg-type]
            reading_order=1,
            provenance=prov(),
        )
        s.add(
            BubbleRow(
                id=bubble.id,
                panel_id=bubble.panel_id,
                chapter_id=chapter_id,
                reading_order=1,
                needs_review=False,
                artifact=bubble.model_dump(mode="json", exclude={"needs_review"}),
            )
        )
    env = Env(
        engine=engine,
        store=store,
        project_id=project_id,
        chapter_id=chapter_id,
        panel_ids=panel_ids,
        detector=FakeDetector(),
        provider=FakeProvider(),
        settings=load_settings(),
    )
    monkeypatch.setattr(uf, "build_deps", env.deps)
    monkeypatch.setattr(uf, "build_engine", lambda: engine)
    return env


def add_job(
    env: Env, run_id: str | None, status: str = "submitted", stage: str = "understand"
) -> str:
    job_id = new_id("job")
    with Session(env.engine) as s, s.begin():
        s.add(
            JobRow(
                id=job_id,
                chapter_id=env.chapter_id,
                flow_run_id=run_id,
                status=status,
                stage=stage,
            )
        )
    return job_id


def job_status(env: Env, job_id: str) -> str:
    with Session(env.engine) as s:
        job = s.get(JobRow, job_id)
        assert job is not None
        return job.status


class Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0.0
        monkeypatch.setattr(uf, "_monotonic", lambda: self.now)


# ---------------------------------------------------------------------------------------------
# the happy path and the gates


def test_scenes_created_for_every_panel_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    job = add_job(env, "run-a")
    summary = env.run(run_id="run-a")
    assert (summary.panels, summary.scenes, summary.cached, summary.failed) == (4, 4, 0, 0)
    assert summary.trial is False
    spec = model_for_stage("understand")
    rows = env.scenes()
    assert len(rows) == 4
    by_panel = {r.panel_id: r for r in rows}
    assert set(by_panel) == set(env.panel_ids)
    for pid, row in by_panel.items():
        scene = SceneJSON.model_validate_json(json.dumps(row.artifact))
        assert scene.panel_id == pid and scene.setting.panel_ids == [pid]
        p = scene.provenance
        assert (p.stage, p.provider, p.tool, p.model_revision) == (
            "understand",
            "local",
            spec.model,
            spec.revision,
        )
        assert p.license == spec.license
        assert p.params_hash == row.params_hash and len(p.input_hashes) == 2
    # the model gets the masked image and no text, on every call
    assert env.provider.loads == 1 and len(env.factory_calls) == 1
    assert len(env.provider.calls) == 4
    for n, (_, content, images) in enumerate(env.provider.calls, 1):
        assert content == "" and len(images) == 1
        assert images[0] != env.store.get(env.panel(env.panel_ids[n - 1]).crop_ref)
    row = env.stage()
    assert (row.status, row.error) == ("done", None) and row.finished_at is not None
    assert row.counts is not None and row.counts["scenes"] == 4 and row.counts["trial"] is False
    assert job_status(env, job) == "completed"


def test_rights_or_not_ready_refuses_and_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    for status in ("pending", "ingesting", "failed"):
        env.update(ChapterRow, env.chapter_id, status=status)
        with pytest.raises(UnderstandRefused, match="chapter is not ready"):
            env.run()
    env.update(ChapterRow, env.chapter_id, status="ready")
    rejected = rights(env.project_id, status="rejected").model_dump(mode="json")
    with Session(env.engine) as s, s.begin():
        row = s.scalar(select(RightsRow).where(RightsRow.project_id == env.project_id))
        assert row is not None
        row.record, row.status = rejected, "rejected"
    with pytest.raises(RightsDenied):
        env.run()
    with Session(env.engine) as s:
        assert s.scalar(select(func.count()).select_from(StageRunRow)) == 0  # nothing claimed
    assert env.factory_calls == [] and env.provider.calls == [] and env.detector.calls == 0
    assert env.scenes() == []
    with pytest.raises(LookupError):
        run_understand("chap_missing", env.deps())


def test_rights_rejected_mid_run_stops_and_keeps_stored_scenes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)

    def revoke(n: int) -> PanelReading | None:
        if n == 2:
            rec = rights(env.project_id, status="rejected").model_dump(mode="json")
            with Session(env.engine) as s, s.begin():
                row = s.scalar(select(RightsRow).where(RightsRow.project_id == env.project_id))
                assert row is not None
                row.record, row.status = rec, "rejected"
        return None

    env.provider.on_call = revoke
    with pytest.raises(RightsDenied):
        env.run()
    assert [r.panel_id for r in env.scenes()] == [env.panel_ids[0]]  # panel 2's write refused
    row = env.stage()
    assert row.status == "failed" and row.error is not None and "rejected" in row.error
    assert len(env.provider.calls) == 2  # panel 3 never reached the model


def test_one_panel_failure_is_review_item_others_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    spec = model_for_stage("understand")

    def fail_second(n: int) -> PanelReading | None:
        if n == 2:
            raise StructuredOutputFailed(spec, 2, "ValidationError")
        return None

    env.provider.on_call = fail_second
    summary = env.run()
    assert (summary.panels, summary.scenes, summary.failed, summary.flagged) == (4, 3, 1, 1)
    assert {r.panel_id for r in env.scenes()} == set(env.panel_ids) - {env.panel_ids[1]}
    (item,) = env.reviews("scene_failed")
    assert item.artifact_id == env.panel_ids[1] and item.status == "open"
    assert "2 attempts" in item.reason and "ValidationError" in item.reason
    assert env.stage().status == "done"


@pytest.mark.parametrize("exc_type", [ValueError, RuntimeError])
def test_provider_value_or_runtime_error_fails_only_that_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
) -> None:
    env = make_env(tmp_path, monkeypatch)

    def refuse_second(n: int) -> PanelReading | None:
        if n == 2:
            raise exc_type(f"image refused {SENTINEL}")  # image refused / out of memory
        return None

    env.provider.on_call = refuse_second
    summary = env.run()
    assert (summary.panels, summary.scenes, summary.failed, summary.flagged) == (4, 3, 1, 1)
    assert len(env.provider.calls) == 4  # the run went on after the failure
    (item,) = env.reviews("scene_failed")
    assert item.artifact_id == env.panel_ids[1]
    assert exc_type.__name__ in item.reason and SENTINEL not in item.reason  # class name only
    assert env.stage().status == "done"


def test_text_claim_stores_scene_and_review_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=1)
    env.provider.on_call = lambda n: reading(weather=Claim(value=OUT_SENTINEL, evidence="text"))
    summary = env.run()
    assert (summary.scenes, summary.failed, summary.flagged) == (1, 0, 1)
    (row,) = env.scenes()
    assert row.artifact["weather"]["value"] is None  # a text-backed claim is dropped
    assert row.artifact["setting"]["value"] == "a quiet room"
    (item,) = env.reviews("scene_text_claim")
    assert item.artifact_id == env.panel_ids[0] and "weather" in item.reason
    assert OUT_SENTINEL not in item.reason


def test_dropped_figures_are_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch, panels=1)
    keep = FigureReading(description=Claim(value="a tall person"), emotion=Claim())
    drop = FigureReading(description=Claim(), emotion=Claim(value="calm"))
    env.provider.on_call = lambda n: reading(figures=[keep, drop])
    summary = env.run()
    assert summary.dropped_figures == 1
    assert env.stage().counts is not None and env.stage().counts["dropped_figures"] == 1  # type: ignore[index]


def test_model_load_failure_fails_stage_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.factory_exc = ModelLoadFailed()
    with pytest.raises(ModelLoadFailed):
        env.run()
    row = env.stage()
    assert (row.status, row.error) == ("failed", "the vision model could not be loaded")
    assert env.scenes() == [] and env.provider.calls == []


def test_interrupted_run_resumes_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)

    def interrupt(n: int) -> PanelReading | None:
        if n == 3:
            raise Interrupted("terminated")
        return None

    env.provider.on_call = interrupt
    with pytest.raises(Interrupted):
        env.run()
    assert len(env.scenes()) == 2
    row = env.stage()
    assert (row.status, row.error) == ("failed", "understand was interrupted")

    env.provider.calls.clear()
    env.provider.on_call = None
    summary = env.run()
    assert len(env.provider.calls) == 2  # panels 3 and 4 only
    assert (summary.scenes, summary.cached) == (4, 2)
    assert env.stage().status == "done"


def test_fully_cached_rerun_loads_no_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.run()
    env.factory_calls.clear()
    env.provider.calls.clear()
    summary = env.run()
    assert env.factory_calls == [] and env.provider.calls == []
    assert (summary.scenes, summary.cached) == (4, 4)


def test_walking_cached_panels_beats_the_stage_every_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=8)
    env.run()  # fill the cache
    clock = Clock(monkeypatch)
    beats: list[float] = []
    real_beat = uf._beat
    monkeypatch.setattr(uf, "_beat", lambda e, r: (beats.append(clock.now), real_beat(e, r)))
    real_cached = uf._cached

    def slow_cached(*a: Any) -> bool:
        clock.now += 30  # a cached panel still costs time (crop, mask, hashes)
        return real_cached(*a)

    monkeypatch.setattr(uf, "_cached", slow_cached)
    summary = env.run()
    assert (summary.cached, env.provider.loads) == (8, 1)  # no model call in the second run
    # one beat at the start, then one per HEARTBEAT_INTERVAL_S of walking (60 s = 2 panels)
    assert beats[0] == 0 and len(beats) >= 4
    gaps = [b - a for a, b in zip(beats, beats[1:], strict=False)]
    assert all(g <= uf.HEARTBEAT_INTERVAL_S + 30 for g in gaps)


def test_prompt_or_settings_change_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=2)
    env.run()
    assert len(env.provider.calls) == 2
    env.settings = env.settings.model_copy(update={"max_new_tokens": 256})
    env.run()
    assert len(env.provider.calls) == 4  # a model setting: everything again
    real = uf.prompt_text
    monkeypatch.setattr(uf, "prompt_text", lambda s: (real(s)[0], "d" * 64))
    env.run()
    assert len(env.provider.calls) == 6  # the prompt changed
    env.run()
    assert len(env.provider.calls) == 6  # nothing changed: cached


def test_a_changed_mask_invalidates_only_that_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=2)
    env.run()
    env.detector.region = Region(BBox(x=0, y=0, w=30, h=30), "speech", 0.9, "bubble", None)
    env.run()
    assert len(env.provider.calls) == 4  # both masks changed: the key follows the sent pixels


def test_rerun_replaces_panel_review_items_without_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=2)
    with Session(env.engine) as s, s.begin():
        s.add(
            ReviewItemRow(
                id=new_id("rev"),
                project_id=env.project_id,
                chapter_id=env.chapter_id,
                artifact_id=env.panel_ids[0],
                kind="ocr_low_confidence",
                reason="ingest item",
                status="open",
            )
        )
    env.provider.on_call = lambda n: reading(weather=Claim(value="x", evidence="text"))
    env.run()
    assert len(env.reviews("scene_text_claim")) == 2
    env.settings = env.settings.model_copy(update={"max_new_tokens": 256})  # recompute all
    env.run()
    assert len(env.reviews("scene_text_claim")) == 2  # replaced, not duplicated
    assert len(env.reviews("ocr_low_confidence")) == 1  # ingest items are not ours to touch
    # a panel that now answers cleanly loses its old item
    env.settings = env.settings.model_copy(update={"max_new_tokens": 128})
    env.provider.on_call = None
    env.run()
    assert env.reviews("scene_text_claim") == []
    assert len(env.scenes()) == 2  # one scene per panel, replaced in place


def test_mask_failure_fails_the_panel_closed_and_calls_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.detector.exc = RuntimeError(f"detector exploded {SENTINEL}")
    summary = env.run()
    assert (summary.scenes, summary.failed) == (0, 4)
    assert env.provider.calls == [] and env.factory_calls == []  # no model, not even loaded
    items = env.reviews("scene_failed")
    assert len(items) == 4 and all("RuntimeError" in i.reason for i in items)
    assert all(SENTINEL not in i.reason for i in items)


def test_unreadable_crop_fails_the_panel_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=2)
    buf = io.BytesIO()
    Image.new("RGB", CROP).save(buf, format="JPEG")
    ref = env.store.put("media", buf.getvalue(), "jpg", "image/jpeg")
    env.update(
        PanelRow,
        env.panel_ids[0],
        artifact={**env.panel(env.panel_ids[0]).model_dump(mode="json"), "crop_ref": ref},
    )
    summary = env.run()
    assert (summary.scenes, summary.failed) == (1, 1) and len(env.provider.calls) == 1


# ---------------------------------------------------------------------------------------------
# budget and trial


def test_budget_stops_with_resumable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    clock = Clock(monkeypatch)
    env.settings = env.settings.model_copy(update={"max_minutes_per_chapter": 1})

    def slow(n: int) -> PanelReading | None:
        clock.now += 40
        return None

    env.provider.on_call = slow
    with pytest.raises(UnderstandRefused, match="time budget"):
        env.run()
    assert len(env.scenes()) == 2  # stored scenes remain
    row = env.stage()
    assert (row.status, row.error) == ("failed", BUDGET)
    clock.now = 0
    env.provider.calls.clear()
    summary = env.run()
    assert len(env.provider.calls) == 2 and summary.scenes == 4  # resumed from the cache
    assert env.stage().status == "done"


def trial_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call_s: float) -> Env:
    env = make_env(tmp_path, monkeypatch)
    clock = Clock(monkeypatch)
    env.provider.on_load = lambda: setattr(clock, "now", clock.now + 10)

    def timed(n: int) -> PanelReading | None:
        clock.now += call_s
        return None

    env.provider.on_call = timed
    return env


def test_trial_limits_panels_records_projection_and_never_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = trial_env(tmp_path, monkeypatch, 5)
    job = add_job(env, "run-t")
    summary = env.run(limit_panels=2, run_id="run-t")
    assert (summary.trial, summary.panels, summary.scenes) == (True, 2, 2)
    assert summary.seconds_per_panel == 5 and summary.projected_total_s == 20  # 10 + 5 * 2
    assert len(env.provider.calls) == 2 and len(env.scenes()) == 2
    row = env.stage()
    assert row.status == "pending" and row.finished_at is not None and row.error is None
    assert row.counts is not None
    c = row.counts
    assert c["trial"] is True and c["limit_panels"] == 2 and c["measured_panels"] == 2
    assert (c["load_s"], c["seconds_per_panel"], c["uncached_remaining"]) == (10, 5, 2)
    assert c["projected_total_s"] == 20 and c["over_budget"] is False
    assert project_total_s(10, 5, 2) == c["projected_total_s"]
    assert job_status(env, job) == "completed"


def test_trial_flags_a_projection_over_the_chapter_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = trial_env(tmp_path, monkeypatch, 40)
    env.settings = env.settings.model_copy(update={"max_minutes_per_chapter": 1})
    env.run(limit_panels=1)  # 10 + 40 * 3 remaining = 130 s > 60 s
    c = env.stage().counts
    assert c is not None and c["projected_total_s"] == 130 and c["over_budget"] is True


@pytest.mark.parametrize("bad", [0, -1, MAX_TRIAL_PANELS + 1])
def test_trial_limit_out_of_range_refused_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: int
) -> None:
    env = make_env(tmp_path, monkeypatch)
    with pytest.raises(UnderstandRefused, match=BAD_TRIAL):
        env.run(limit_panels=bad)
    with Session(env.engine) as s:
        assert s.scalar(select(func.count()).select_from(StageRunRow)) == 0
    assert env.factory_calls == []


def test_full_run_after_trial_reuses_trial_scenes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.run(limit_panels=2)
    assert len(env.provider.calls) == 2
    summary = env.run()
    assert len(env.provider.calls) == 4 and (summary.scenes, summary.cached) == (4, 2)
    assert env.stage().status == "done" and env.stage().counts["trial"] is False  # type: ignore[index]


def test_project_total_s() -> None:
    assert project_total_s(30.0, 12.5, 10) == 155.0
    assert project_total_s(30.0, 12.5, 0) == 30.0


# ---------------------------------------------------------------------------------------------
# ownership, heartbeat, hooks


def test_live_run_refused_stale_taken_over_and_old_owner_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=2)
    now = datetime.now(UTC)
    with Session(env.engine) as s, s.begin():
        s.add(
            StageRunRow(
                chapter_id=env.chapter_id,
                stage="understand",
                status="running",
                run_id="run-old",
                started_at=now,
                heartbeat_at=now,
            )
        )
    with pytest.raises(UnderstandAlreadyRunning, match="understand already running"):
        env.run(run_id="run-new")
    row = env.stage()
    assert (row.status, row.run_id) == ("running", "run-old")  # untouched
    assert env.factory_calls == [] and env.scenes() == []

    env.update(
        StageRunRow,
        (env.chapter_id, "understand"),
        heartbeat_at=now - timedelta(seconds=STALE_INGEST_S + 60),
    )
    env.run(run_id="run-new")
    assert (env.stage().status, env.stage().run_id) == ("done", "run-new")

    # the old owner wakes up: every write path refuses
    old = uf._Run(env.chapter_id, env.project_id, "run-old")
    with pytest.raises(UnderstandAlreadyRunning):
        uf._beat(env.engine, old)
    with pytest.raises(UnderstandAlreadyRunning):
        uf._store_failure(env.engine, old, env.panel_ids[0], "x")
    uf._fail(env.engine, env.chapter_id, "late", "run-old")
    assert env.stage().status == "done" and env.stage().error is None


def test_a_run_taken_over_mid_run_writes_nothing_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)

    def taken(n: int) -> PanelReading | None:
        if n == 2:
            env.update(StageRunRow, (env.chapter_id, "understand"), run_id="run-b")
        return None

    env.provider.on_call = taken
    with pytest.raises(UnderstandAlreadyRunning):
        env.run(run_id="run-a")
    assert len(env.scenes()) == 1
    row = env.stage()
    assert (row.status, row.run_id, row.error) == ("running", "run-b", None)


def test_heartbeat_thread_beats_while_a_panel_generates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=1)
    monkeypatch.setattr(uf, "HEARTBEAT_INTERVAL_S", 0.05)
    seen: list[datetime | None] = []

    def slow(n: int) -> PanelReading | None:
        seen.append(env.stage().heartbeat_at)
        time.sleep(0.5)
        seen.append(env.stage().heartbeat_at)
        return None

    env.provider.on_call = slow
    env.run()
    assert seen[0] is not None and seen[1] is not None and seen[1] > seen[0]
    assert not any(t.name == "understand-heartbeat" for t in threading.enumerate())  # stopped


def test_heartbeat_thread_stops_the_run_when_ownership_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(uf, "HEARTBEAT_INTERVAL_S", 0.05)

    def lose(n: int) -> PanelReading | None:
        if n == 1:
            env.update(StageRunRow, (env.chapter_id, "understand"), run_id="run-b")
            time.sleep(0.4)  # the thread sees the loss meanwhile
        return None

    env.provider.on_call = lose
    with pytest.raises(UnderstandAlreadyRunning):
        env.run(run_id="run-a")
    assert env.scenes() == [] and len(env.provider.calls) == 1
    assert not any(t.name == "understand-heartbeat" for t in threading.enumerate())


def test_chapter_changed_mid_run_stops_with_a_safe_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)

    def changed(n: int) -> PanelReading | None:
        if n == 2:
            env.update(ChapterRow, env.chapter_id, status="ingesting")
        return None

    env.provider.on_call = changed
    with pytest.raises(UnderstandRefused, match="changed during understanding"):
        env.run()
    assert len(env.scenes()) == 1
    assert env.stage().error == "the chapter changed during understanding"


def test_hooks_only_touch_owned_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    other = SimpleNamespace(id="run-b", parameters={"chapter_id": env.chapter_id})
    own = SimpleNamespace(id="run-a", parameters={"chapter_id": env.chapter_id})

    def during(n: int) -> PanelReading | None:
        if n == 1:
            uf.on_cancelled(understand_flow, other, None)  # type: ignore[arg-type]
            assert env.stage().status == "running"  # not the owner: nothing changes
            uf.on_crashed(understand_flow, own, None)  # type: ignore[arg-type]
        return None

    env.provider.on_call = during
    with pytest.raises(UnderstandAlreadyRunning):  # crashed hook took the stage: no more writes
        env.run(run_id="run-a")
    row = env.stage()
    assert (row.status, row.error) == ("failed", "understand was interrupted")
    uf.on_failed(understand_flow, own, None)  # no longer running: the message stays
    assert env.stage().error == "understand was interrupted"


def test_unstarted_run_fails_a_pending_stage_only_for_its_own_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.run(limit_panels=1)  # leaves the stage 'pending' with its trial counts
    old = add_job(env, "run-old", status="failed")
    ingest_job = add_job(env, "run-ingest", stage="ingest")  # newer, but not ours
    queued = add_job(env, "run-q")
    run = SimpleNamespace(id="run-old", parameters={"chapter_id": env.chapter_id})
    uf.on_crashed(understand_flow, run, None)  # type: ignore[arg-type]
    assert env.stage().status == "pending"  # a newer understand job exists: leave it
    q = SimpleNamespace(id="run-q", parameters={"chapter_id": env.chapter_id})
    uf.on_cancelled(understand_flow, q, None)  # type: ignore[arg-type]
    row = env.stage()
    assert (row.status, row.error) == ("failed", "understand could not start")
    assert (job_status(env, queued), job_status(env, old)) == ("cancelled", "failed")
    assert job_status(env, ingest_job) == "submitted"


def test_build_deps_failure_fails_the_pending_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.run(limit_panels=1)
    jobs: list[str] = []

    def broken() -> UnderstandDeps:
        jobs.append(add_job(env, str(prefect_flow_run.id)))
        raise RuntimeError("object store credentials missing")

    monkeypatch.setattr(uf, "build_deps", broken)
    with pytest.raises(RuntimeError, match="credentials"):
        understand_flow(env.chapter_id)
    row = env.stage()
    assert (row.status, row.error) == ("failed", "understand could not start")
    assert job_status(env, jobs[0]) == "failed"


def test_flow_hooks_timeout_and_parameters() -> None:
    assert uf.on_crashed in understand_flow.on_crashed_hooks
    assert uf.on_cancelled in understand_flow.on_cancellation_hooks
    assert uf.on_failed in understand_flow.on_failure_hooks
    assert understand_flow.timeout_seconds == 13 * 3600
    assert understand_flow.timeout_seconds > load_settings().max_minutes_per_chapter * 60
    assert uf.FLOW_NAME == "chapter-understand"
    params = understand_flow.validate_parameters({"chapter_id": "chap_1", "limit_panels": 5})
    assert params["limit_panels"] == 5
    assert understand_flow.validate_parameters({"chapter_id": "chap_1"})["limit_panels"] is None
    with pytest.raises(Exception, match="limit_panels"):
        understand_flow.validate_parameters({"chapter_id": "chap_1", "limit_panels": "many"})


def test_timed_out_run_marks_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)

    def hang(n: int) -> PanelReading | None:
        for _ in range(100):  # 10 s at most; the timeout fires between the short sleeps
            time.sleep(0.1)
        return None

    env.provider.on_call = hang
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        understand_flow.with_options(timeout_seconds=1)(env.chapter_id)
    assert time.monotonic() - started < 8
    row = env.stage()
    assert (row.status, row.error) == ("failed", "understand timed out")
    assert env.scenes() == []


def test_flow_run_id_is_the_owner_token_and_job_follows_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, panels=1)
    seen: list[Any] = []

    def record(n: int) -> PanelReading | None:
        seen.extend([env.stage().run_id, prefect_flow_run.id, job_status(env, job_for_run[0])])
        return None

    job_for_run: list[str] = []
    env.provider.on_call = record
    # a Prefect run: the job row is created by the API with the flow-run id; emulate it lazily
    original = uf.build_deps

    def deps_with_job() -> UnderstandDeps:
        job_for_run.append(add_job(env, str(prefect_flow_run.id)))
        return original()

    monkeypatch.setattr(uf, "build_deps", deps_with_job)
    understand_flow(env.chapter_id)
    assert seen[0] is not None and seen[0] == seen[1] and seen[2] == "running"
    assert job_status(env, job_for_run[0]) == "completed"
    direct = env.stage().run_id
    run_understand(env.chapter_id, original())  # direct calls get a fresh token each time
    assert env.stage().run_id not in (direct, None)


def test_failing_fail_does_not_mask_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.provider.on_call = lambda n: (_ for _ in ()).throw(OSError("model exploded"))

    def broken(*a: Any, **k: Any) -> None:
        raise OSError("database went away")

    monkeypatch.setattr(uf, "_fail", broken)
    with pytest.raises(OSError, match="model exploded"):
        env.run()


def test_errors_and_logs_carry_no_ocr_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env = make_env(tmp_path, monkeypatch)
    spec = model_for_stage("understand")

    def fail(n: int) -> PanelReading | None:
        if n == 1:
            raise StructuredOutputFailed(spec, 2, "ValidationError")
        if n == 2:
            return reading(setting=Claim(value=OUT_SENTINEL, evidence="text"))
        raise OSError(f"boom {SENTINEL} {OUT_SENTINEL}")

    env.provider.on_call = fail
    with pytest.raises(OSError):
        env.run()
    assert (
        SENTINEL not in caplog.text and OUT_SENTINEL not in caplog.text
    )  # only the raised error has them
    row = env.stage()
    assert row.status == "failed" and row.error == "understand failed"
    assert SENTINEL not in (row.error or "") and OUT_SENTINEL not in (row.error or "")
    for item in env.reviews():
        assert SENTINEL not in item.reason and OUT_SENTINEL not in item.reason
    # nothing of the OCR text ever reached the model
    assert all(SENTINEL not in c[0] + c[1] for c in env.provider.calls)


def test_api_trial_bound_equals_the_flow_bound() -> None:
    from kagebox_api.routes.chapters import MAX_TRIAL_PANELS as API_MAX

    assert API_MAX == MAX_TRIAL_PANELS  # the API cannot import the flow, so the two are pinned here
