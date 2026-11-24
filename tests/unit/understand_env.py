"""Shared fakes and the SQLite-backed Env for the understand flow, replay and report tests."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import orchestration.understand_flow as uf
from orchestration.understand_flow import (
    UnderstandDeps,
    run_understand,
)
from pipeline.common.database import make_engine
from pipeline.common.dbmodels import (
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
    RightsRecord,
)
from pipeline.common.settings import Settings
from pipeline.common.storage import MemoryObjectStore
from pipeline.ingest.bubbles import Region
from pipeline.providers.base import ModelSpec
from pipeline.understand.contract import Claim, PanelReading
from pipeline.understand.settings import load_settings

EVIDENCE = "evidence/sha256/" + "a" * 64 + ".pdf"
SENTINEL = "SENTINEL-OCR-31ce"
OUT_SENTINEL = "SENTINEL-OUT-8d02"
CROP = (60, 40)


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

    def stage_missing(self) -> bool:
        with Session(self.engine) as s:
            return s.get(StageRunRow, (self.chapter_id, "understand")) is None

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
