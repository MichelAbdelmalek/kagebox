"""The chapter-ingest flow: SQLite, in-memory object store, fake detector and OCR (no models)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw
from prefect import flow
from prefect.runtime import flow_run as prefect_flow_run
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

import orchestration.ingest_flow as ingest_mod
from orchestration.flows import run_chapter
from orchestration.ingest_flow import IngestDeps, IngestSummary, ingest_flow
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
    User,
)
from pipeline.common.ids import new_id
from pipeline.common.schemas import BBox, Bubble, Page, Panel, RightsDenied, RightsRecord
from pipeline.common.settings import Settings
from pipeline.common.storage import MemoryObjectStore
from pipeline.common.uploads import UploadRejected
from pipeline.ingest.bubbles import Region
from pipeline.ingest.panels import slice_strip
from pipeline.ingest.process import params_hash

EVIDENCE = "evidence/sha256/" + "a" * 64 + ".pdf"
W, H = 600, 800
# Two framed panels stacked vertically; the gutter between them is y 380..420.
PANEL_BOXES = [(20, 20, 580, 380), (20, 420, 580, 780)]
IN_TOP = Region(
    BBox(x=100, y=100, w=80, h=60), "speech", 0.9, "bubble", BBox(x=110, y=110, w=60, h=40)
)
IN_BOTTOM = Region(BBox(x=300, y=500, w=90, h=50), "caption", 0.8, "text_free", None)
IN_TOP_RIGHT = Region(BBox(x=400, y=100, w=60, h=40), "speech", 0.9, "bubble", None)
IN_GUTTER = Region(BBox(x=200, y=388, w=50, h=20), "speech", 0.7, "bubble", None)


# ---------------------------------------------------------------------------------------------
# fixtures and fakes


def page_png(
    boxes: list[tuple[int, int, int, int]] = PANEL_BOXES, size: tuple[int, int] = (W, H)
) -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    for box in boxes:
        draw.rectangle(box, outline="black", width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_cbz(pages: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, data in enumerate(pages, 1):
            zf.writestr(f"{n:03d}.png", data)
    return buf.getvalue()


class FakeDetector:
    def __init__(self, regions: list[Region]) -> None:
        self.regions = regions

    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]:
        return [r for r in self.regions if r.score >= threshold]


class Interrupted(BaseException):
    """Stands in for Prefect's TerminationSignal (a BaseException, not an Exception)."""


@dataclass
class FakeOcr:
    engine: str = "manga-ocr"
    models: tuple[str, ...] = ("manga-ocr",)
    results: list[tuple[str, float]] = field(default_factory=list)  # consumed in order
    default: tuple[str, float] = ("テスト", 0.97)
    fail_on_call: int | None = None
    interrupt_on_call: int | None = None
    on_call: Callable[[int], None] | None = None
    calls: int = 0

    def read(self, crop: Image.Image) -> tuple[str, float]:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("model exploded: /secret/path details")
        if self.interrupt_on_call is not None and self.calls == self.interrupt_on_call:
            raise Interrupted("terminated")
        return self.results.pop(0) if self.results else self.default


@dataclass
class Env:
    engine: Engine
    store: MemoryObjectStore
    imports: Path
    project_id: str
    detector: FakeDetector
    ocr: FakeOcr
    ocr_languages: list[str] = field(default_factory=list)


@pytest.fixture(scope="module", autouse=True)
def harness() -> Iterator[None]:
    with prefect_test_harness(server_startup_timeout=120):
        yield


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


def make_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str = "manga_rtl", **rec: Any
) -> Env:
    engine = make_engine(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))
    Base.metadata.create_all(engine)
    imports = tmp_path / "imports"
    imports.mkdir()
    user_id, project_id = new_id("user"), new_id("proj")
    record = rights(project_id, **rec)
    with Session(engine) as s, s.begin():
        s.add(User(id=user_id, email="a@b.c", password_hash="x"))
        s.flush()
        s.add(ProjectRow(id=project_id, title="T", format=fmt, created_by=user_id))
        s.flush()
        s.add(
            RightsRow(
                id=record.id,
                project_id=project_id,
                status=record.status,
                record=record.model_dump(mode="json"),
            )
        )
    env = Env(
        engine=engine,
        store=MemoryObjectStore(),
        imports=imports,
        project_id=project_id,
        detector=FakeDetector([IN_TOP, IN_TOP_RIGHT, IN_BOTTOM, IN_GUTTER]),
        ocr=FakeOcr(),
    )

    def ocr_factory(language: str) -> FakeOcr:
        env.ocr_languages.append(language)
        return env.ocr

    deps = IngestDeps(
        engine=engine,
        store=env.store,
        imports_dir=imports,
        detector_factory=lambda: env.detector,
        ocr_factory=ocr_factory,
    )
    monkeypatch.setattr(ingest_mod, "build_deps", lambda: deps)
    monkeypatch.setattr(ingest_mod, "build_engine", lambda: engine)
    return env


def add_chapter(
    env: Env,
    data: bytes,
    *,
    name: str = "ch.cbz",
    kind: str = "import",
    page_start: int = 1,
    page_end: int = 2,
    fmt: str = "manga_rtl",
    sha: str | None = None,
) -> str:
    if kind == "import":
        folder = env.imports / env.project_id  # imports are scoped per project
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(data)
        ref = name
    else:
        ref = env.store.put("media", data, "cbz", "application/zip")
    chapter_id = new_id("chap")
    with Session(env.engine) as s, s.begin():
        s.add(
            ChapterRow(
                id=chapter_id,
                project_id=env.project_id,
                number=1,
                language="ja",
                format=fmt,
                source_kind=kind,
                source_ref=ref,
                source_sha256=sha or hashlib.sha256(data).hexdigest(),
                page_start=page_start,
                page_end=page_end,
                status="pending",
            )
        )
    return chapter_id


def chapter(env: Env, chapter_id: str) -> ChapterRow:
    with Session(env.engine) as s:
        row = s.get(ChapterRow, chapter_id)
        assert row is not None
        return row


def count(env: Env, model: Any, chapter_id: str) -> int:
    with Session(env.engine) as s:
        return int(
            s.scalar(select(func.count()).select_from(model).where(model.chapter_id == chapter_id))
            or 0
        )


def reviews(env: Env, chapter_id: str) -> list[ReviewItemRow]:
    with Session(env.engine) as s:
        return list(s.scalars(select(ReviewItemRow).where(ReviewItemRow.chapter_id == chapter_id)))


def kinds(env: Env, chapter_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in reviews(env, chapter_id):
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


# ---------------------------------------------------------------------------------------------
# the six brief tests


def test_ingest_creates_pages_panels_bubbles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    src = make_cbz([page_png(), page_png()])
    cid = add_chapter(env, src)

    summary = ingest_flow(cid)

    assert summary == IngestSummary(pages=2, panels=4, bubbles=6, flagged=2)
    row = chapter(env, cid)
    assert (row.status, row.error) == ("ready", None)
    assert env.ocr_languages == ["ja"]  # one OCR instance per run, for the chapter's language
    src_hash = hashlib.sha256(src).hexdigest()
    with Session(env.engine) as s:
        pages = list(
            s.scalars(select(PageRow).where(PageRow.chapter_id == cid).order_by(PageRow.index))
        )
        assert [p.index for p in pages] == [1, 2]
        for n, prow in enumerate(pages, 1):
            page = Page.model_validate_json(json.dumps(prow.artifact))
            assert (page.width, page.height, page.chapter_id) == (W, H, cid)
            assert (page.source_page, page.source_y0, page.source_y1) == (n, None, None)
            params = {"tool": "kagebox-ingest", "direction": "rtl", "fit": "long_side"}
            assert page.provenance.params_hash == params_hash(params)
            assert page.provenance.stage == "ingest"
            assert page.provenance.tool == "kagebox-ingest"
            assert page.provenance.input_hashes == [src_hash]
            img = Image.open(io.BytesIO(env.store.get(page.image_ref)))
            assert img.size == (W, H)

            panels = list(
                s.scalars(
                    select(PanelRow)
                    .where(PanelRow.page_id == prow.id)
                    .order_by(PanelRow.reading_order)
                )
            )
            assert [p.reading_order for p in panels] == [1, 2]
            parsed = [Panel.model_validate_json(json.dumps(p.artifact)) for p in panels]
            assert parsed[0].bbox.y < parsed[1].bbox.y  # top panel first
            assert parsed[0].provenance.input_hashes[0] == src_hash
            assert len(parsed[0].provenance.input_hashes) == 2  # source + page image
            crop = Image.open(io.BytesIO(env.store.get(parsed[0].crop_ref)))
            assert crop.size == (parsed[0].bbox.w, parsed[0].bbox.h)

            top = list(
                s.scalars(
                    select(BubbleRow)
                    .where(BubbleRow.panel_id == panels[0].id)
                    .order_by(BubbleRow.reading_order)
                )
            )
            bubbles = [Bubble.model_validate_json(json.dumps(b.artifact)) for b in top]
            # manga reads right to left: the right-hand balloon first
            assert [b.bbox for b in bubbles] == [IN_TOP_RIGHT.bbox, IN_TOP.bbox]
            assert [b.reading_order for b in bubbles] == [r.reading_order for r in top] == [1, 2]
            b = bubbles[1]
            assert (b.kind, b.text, b.lang, b.ocr_engine, b.speaker.value) == (
                "speech",
                "テスト",
                "ja",
                "manga-ocr",
                None,
            )
            assert b.provenance.tool == "manga-ocr"
            assert b.provenance.model_revision == "aa6573bd10b0d446cbf622e29c3e084914df9741"
            assert b.provenance.license == "CC-BY-SA-4.0"
            assert [c.name for c in b.provenance.components] == [
                "manga-bubble-detector",
                "manga-ocr",
            ]
            detector = b.provenance.components[0]
            assert detector.revision == "bcf342f296beb56005e044da15990502629015de"
            assert detector.license == "Apache-2.0"
            assert detector.source_url is not None
            assert not top[1].needs_review
            bottom = s.scalars(select(BubbleRow).where(BubbleRow.panel_id == panels[1].id)).one()
            assert bottom.artifact["kind"] == "caption"
    # the gutter region becomes a review item on its page, never a bubble (ruling R1)
    items = reviews(env, cid)
    assert {r.kind for r in items} == {"region_outside_panels"}
    assert {r.artifact_id for r in items} == {p.id for p in pages}
    assert all("x=200" in r.reason and r.status == "open" for r in items)


def test_rights_not_approved_refuses_and_marks_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch, status="pending", approved_by=None, approved_at=None)
    cid = add_chapter(env, make_cbz([page_png(), page_png()]))

    with pytest.raises(RightsDenied):
        ingest_flow(cid)

    row = chapter(env, cid)
    assert row.status == "failed"
    assert row.error == "rights record is pending, not approved"
    assert env.ocr.calls == 0
    assert count(env, PageRow, cid) == 0


def test_low_confidence_creates_review_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.detector.regions = [IN_TOP]
    env.ocr.results = [("かすれ", 0.42)]
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)

    summary = ingest_flow(cid)

    assert summary.flagged == 1
    [item] = reviews(env, cid)
    with Session(env.engine) as s:
        bubble = s.scalars(select(BubbleRow).where(BubbleRow.chapter_id == cid)).one()
    assert (item.kind, item.artifact_id, item.project_id) == (
        "ocr_low_confidence",
        bubble.id,
        env.project_id,
    )
    assert "0.42" in item.reason
    assert bubble.needs_review is True
    assert "needs_review" not in bubble.artifact


def test_rerun_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png(), page_png()]))

    first = ingest_flow(cid)
    with Session(env.engine) as s:
        old_pages = set(s.scalars(select(PageRow.id).where(PageRow.chapter_id == cid)))
    second = ingest_flow(cid)

    assert first == second
    assert count(env, PageRow, cid) == 2
    assert count(env, PanelRow, cid) == 4
    assert count(env, BubbleRow, cid) == 6
    assert len(reviews(env, cid)) == 2
    with Session(env.engine) as s:
        assert not old_pages & set(s.scalars(select(PageRow.id).where(PageRow.chapter_id == cid)))
    assert chapter(env, cid).status == "ready"


def test_failure_leaves_no_partial_ready_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png(), page_png()]))
    env.ocr.fail_on_call = 4  # page 2's first bubble: page 1 was fully processed

    with pytest.raises(RuntimeError):
        ingest_flow(cid)

    row = chapter(env, cid)
    assert (row.status, row.error) == ("failed", "ingest failed")  # no details, no traceback
    for model in (PageRow, PanelRow, BubbleRow):
        assert count(env, model, cid) == 0
    assert reviews(env, cid) == []

    # a good run, then a failing re-run: the old complete set survives, status is failed
    env.ocr.fail_on_call, env.ocr.calls = None, 0
    ingest_flow(cid)
    env.ocr.fail_on_call, env.ocr.calls = 2, 0
    with pytest.raises(RuntimeError):
        ingest_flow(cid)
    assert chapter(env, cid).status == "failed"
    assert (count(env, PageRow, cid), count(env, BubbleRow, cid)) == (2, 6)


def test_manhwa_strip_sliced_into_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch, fmt="manhwa_vertical")
    env.detector.regions = []
    tall = (400, 7000)
    boxes = [(10, y, 390, y + 1500) for y in (50, 1800, 3550, 5300)]
    strip = page_png(boxes, size=tall)
    cid = add_chapter(env, make_cbz([strip]), page_end=1, fmt="manhwa_vertical")

    summary = ingest_flow(cid)

    expected = slice_strip(np.asarray(Image.open(io.BytesIO(strip)).convert("RGB")))
    assert len(expected) >= 3
    assert summary.pages == len(expected)
    with Session(env.engine) as s:
        pages = [
            Page.model_validate_json(json.dumps(p.artifact))
            for p in s.scalars(
                select(PageRow).where(PageRow.chapter_id == cid).order_by(PageRow.index)
            )
        ]
    assert [p.index for p in pages] == list(range(1, len(expected) + 1))
    assert [p.height for p in pages] == [y1 - y0 for y0, y1 in expected]
    assert [(p.source_page, p.source_y0, p.source_y1) for p in pages] == [
        (1, y0, y1) for y0, y1 in expected
    ]
    assert all(p.width == 400 for p in pages)
    assert chapter(env, cid).status == "ready"

    # a manhwa PDF is refused before anything is rendered (ruling T2)
    pdf = add_chapter(
        env, b"%PDF-1.7\n" + b"0" * 64, name="strip.pdf", page_end=1, fmt="manhwa_vertical"
    )
    with pytest.raises(UploadRejected):
        ingest_flow(pdf)
    row = chapter(env, pdf)
    assert (row.status, row.error) == ("failed", "manhwa sources must be images or CBZ")


# ---------------------------------------------------------------------------------------------
# sources, review kinds and wiring


def test_upload_source_from_object_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png(), page_png()]), kind="upload")
    assert ingest_flow(cid).pages == 2


def test_single_image_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    data = page_png()
    ref = env.store.put("media", data, "png", "image/png")
    cid = add_chapter(env, make_cbz([data]), kind="upload", page_end=1)
    with Session(env.engine) as s, s.begin():
        row = s.get(ChapterRow, cid)
        assert row is not None
        row.source_ref, row.source_sha256 = ref, hashlib.sha256(data).hexdigest()
    assert ingest_flow(cid).pages == 1


@pytest.mark.parametrize(
    "name", ["../ch.cbz", "/etc/passwd", "a/b.cbz", "..", ".", "", "a\\b.cbz", "x\x00y"]
)
def test_import_name_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    with Session(env.engine) as s, s.begin():
        row = s.get(ChapterRow, cid)
        assert row is not None
        row.source_ref = name
    with pytest.raises(UploadRejected):
        ingest_flow(cid)
    assert chapter(env, cid).error == "the imported source file is not available"


def test_import_symlink_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    own = env.imports / env.project_id
    (own / "ch.cbz").rename(own / "real.cbz")
    (own / "ch.cbz").symlink_to(own / "real.cbz")
    with pytest.raises(UploadRejected):
        ingest_flow(cid)
    assert chapter(env, cid).status == "failed"


@pytest.mark.parametrize("where", ["flat", "other_project", "symlinked_dir"])
def test_import_only_from_the_projects_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    env = make_env(tmp_path, monkeypatch)
    data = make_cbz([page_png()])
    cid = add_chapter(env, data, page_end=1)
    own = env.imports / env.project_id
    (own / "ch.cbz").unlink()
    own.rmdir()
    if where == "flat":
        (env.imports / "ch.cbz").write_bytes(data)
    elif where == "other_project":
        other = env.imports / new_id("proj")
        other.mkdir()
        (other / "ch.cbz").write_bytes(data)
    else:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "ch.cbz").write_bytes(data)
        own.symlink_to(elsewhere)
    with pytest.raises(UploadRejected):
        ingest_flow(cid)
    row = chapter(env, cid)
    assert (row.status, row.error) == ("failed", "the imported source file is not available")


def test_source_hash_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1, sha="b" * 64)
    with pytest.raises(UploadRejected):
        ingest_flow(cid)
    row = chapter(env, cid)
    assert (row.status, row.error) == ("failed", "the source file changed since it was added")
    assert env.ocr.calls == 0


def test_panel_and_text_review_kinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.detector.regions = [IN_TOP]
    env.ocr.default = ("", 0.0)
    blank = page_png([])  # no frames: one whole-page panel
    cid = add_chapter(env, make_cbz([blank]), page_end=1)

    ingest_flow(cid)

    assert kinds(env, cid) == {"panel_full_page": 1, "ocr_low_confidence": 1, "page_no_text": 1}


def test_irregular_panel_threshold_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.detector.regions = []
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    monkeypatch.setattr(ingest_mod, "load_thresholds", lambda: {"panel_irregular_fill": 1.01})
    ingest_flow(cid)
    assert kinds(env, cid) == {"panel_irregular": 2}


def test_merged_panel_always_goes_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An L-shaped frame and a frame in its notch: two components, one merged panel (ruling P4).
    env = make_env(tmp_path, monkeypatch)
    env.detector.regions = []
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    l_shape = [(40, 40), (W - 40, 40), (W - 40, H - 40), (W // 2 + 20, H - 40)]
    l_shape += [(W // 2 + 20, H * 2 // 3), (40, H * 2 // 3)]
    draw.line([*l_shape, l_shape[0]], fill="black", width=4)
    draw.rectangle((40, H * 2 // 3 + 30, W // 2 - 20, H - 40), outline="black", width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    cid = add_chapter(env, make_cbz([buf.getvalue()]), page_end=1)
    # Fill threshold 0: panel_irregular can never fire, panel_merged must anyway.
    monkeypatch.setattr(ingest_mod, "load_thresholds", lambda: {"panel_irregular_fill": 0.0})
    ingest_flow(cid)
    assert kinds(env, cid) == {"panel_merged": 1}
    with Session(env.engine) as s:
        [item] = s.scalars(select(ReviewItemRow).where(ReviewItemRow.chapter_id == cid)).all()
        [panel] = s.scalars(select(PanelRow).where(PanelRow.chapter_id == cid)).all()
    assert item.artifact_id == panel.id
    assert "2 components" in item.reason and "fills" in item.reason

    ingest_flow(cid)  # a re-run replaces the item instead of adding a second one
    assert kinds(env, cid) == {"panel_merged": 1}


def test_thresholds_yaml_has_panel_irregular_fill() -> None:
    assert ingest_mod.load_thresholds()["panel_irregular_fill"] == 0.85


def test_chapter_pipeline_ingest_stage_runs_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    record = rights(env.project_id)

    @flow
    def pipeline() -> list[dict[str, str]]:
        runners = {"ingest": ingest_mod.ingest_stage}
        return run_chapter(lambda _: record, env.project_id, cid, "gpu", runners=runners)

    out = pipeline()
    assert [r["status"] for r in out[1:]] == ["not_implemented"] * 7
    assert out[0] == {
        "stage": "ingest",
        "chapter_id": cid,
        "status": "ready",
        "pages": "1",
        "panels": "2",
        "bubbles": "3",
        "flagged": "1",
    }
    assert chapter(env, cid).status == "ready"


def test_deployment_parameters_validate() -> None:
    assert ingest_flow.name == "chapter-ingest"
    assert ingest_flow.validate_parameters({"chapter_id": "chap_1"}) == {"chapter_id": "chap_1"}
    with pytest.raises(Exception, match="chapter_id"):
        ingest_flow.validate_parameters({})


# ---------------------------------------------------------------------------------------------
# fix round 1: source position, provenance components, interruption, locking, invalid OCR


def test_index_is_reading_position_and_source_page_is_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    env.detector.regions = []
    cid = add_chapter(env, make_cbz([page_png()] * 4), page_start=3, page_end=4)
    ingest_flow(cid)
    with Session(env.engine) as s:
        pages = [
            Page.model_validate_json(json.dumps(p.artifact))
            for p in s.scalars(
                select(PageRow).where(PageRow.chapter_id == cid).order_by(PageRow.index)
            )
        ]
    assert [(p.index, p.source_page, p.source_y0) for p in pages] == [(1, 3, None), (2, 4, None)]


def test_pp_ocr_components_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch, fmt="comic_ltr")
    env.detector.regions = [IN_TOP]
    env.ocr.engine, env.ocr.models = "pp-ocrv5-rec-en", ("pp-ocrv5-det", "pp-ocrv5-rec-en")
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1, fmt="comic_ltr")
    ingest_flow(cid)
    with Session(env.engine) as s:
        row = s.scalars(select(BubbleRow).where(BubbleRow.chapter_id == cid)).one()
    prov = Bubble.model_validate_json(json.dumps(row.artifact)).provenance
    assert (prov.tool, prov.model_revision) == (
        "pp-ocrv5-rec-en",
        "e4abce2bbd335f6c3eda0507b7f8e8abd5af3419",
    )
    assert [c.name for c in prov.components] == [
        "manga-bubble-detector",
        "pp-ocrv5-det",
        "pp-ocrv5-rec-en",
    ]


def test_interrupted_run_marks_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png(), page_png()]))
    env.ocr.interrupt_on_call = 2
    with pytest.raises(Interrupted):
        ingest_mod.run_ingest(cid, ingest_mod.build_deps())
    row = chapter(env, cid)
    assert (row.status, row.error) == ("failed", "ingest was interrupted")
    assert count(env, PageRow, cid) == 0


def test_flow_hooks_registered() -> None:
    assert ingest_mod.on_crashed in ingest_flow.on_crashed_hooks
    assert ingest_mod.on_cancelled in ingest_flow.on_cancellation_hooks
    assert ingest_mod.on_failed in ingest_flow.on_failure_hooks


def test_hook_for_other_run_leaves_owner_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run B (cancelled while waiting, a subflow, a stale pair) must not fail run A's chapter."""
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    other = SimpleNamespace(id="run-b", parameters={"chapter_id": cid})

    def b_interrupted(call: int) -> None:
        if call == 1:
            ingest_mod.on_cancelled(ingest_flow, other, None)  # type: ignore[arg-type]
            assert chapter(env, cid).status == "ingesting"

    env.ocr.on_call = b_interrupted
    summary = ingest_mod.run_ingest(cid, ingest_mod.build_deps(), run_id="run-a")
    assert summary.pages == 1
    row = chapter(env, cid)
    assert (row.status, row.error, row.ingest_run_id) == ("ready", None, "run-a")


def test_hook_for_owning_run_marks_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    own = SimpleNamespace(id="run-a", parameters={"chapter_id": cid})

    def a_crashed(call: int) -> None:
        if call == 1:
            ingest_mod.on_crashed(ingest_flow, own, None)  # type: ignore[arg-type]

    env.ocr.on_call = a_crashed
    with pytest.raises(ingest_mod.IngestAlreadyRunning):  # A no longer owns it: writes nothing
        ingest_mod.run_ingest(cid, ingest_mod.build_deps(), run_id="run-a")
    row = chapter(env, cid)
    assert (row.status, row.error) == ("failed", "ingest was interrupted")
    assert count(env, PageRow, cid) == 0
    # a chapter that is no longer ingesting keeps its message
    ingest_mod.on_crashed(ingest_flow, own, None)  # type: ignore[arg-type]
    assert chapter(env, cid).error == "ingest was interrupted"


def add_job(env: Env, chapter_id: str, run_id: str | None, status: str = "submitted") -> str:
    job_id = new_id("job")
    with Session(env.engine) as s, s.begin():
        s.add(JobRow(id=job_id, chapter_id=chapter_id, flow_run_id=run_id, status=status))
    return job_id


def job_status(env: Env, job_id: str) -> str:
    with Session(env.engine) as s:
        job = s.get(JobRow, job_id)
        assert job is not None
        return job.status


def test_job_follows_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    job = add_job(env, cid, "run-a")
    seen: list[str] = []
    env.ocr.on_call = lambda call: seen.append(job_status(env, job))
    ingest_mod.run_ingest(cid, ingest_mod.build_deps(), run_id="run-a")
    assert seen[0] == "running" and job_status(env, job) == "completed"

    failing = add_chapter(env, make_cbz([page_png()]), page_end=1)
    job2 = add_job(env, failing, "run-b")
    env.ocr.on_call, env.ocr.fail_on_call, env.ocr.calls = None, 1, 0
    with pytest.raises(RuntimeError):
        ingest_mod.run_ingest(failing, ingest_mod.build_deps(), run_id="run-b")
    assert job_status(env, job2) == "failed"


def test_build_deps_failure_fails_the_pending_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run fails before it takes the chapter: no 'pending' chapter left forever."""
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    jobs: list[str] = []

    def broken_deps() -> IngestDeps:
        jobs.append(add_job(env, cid, str(prefect_flow_run.id)))  # as the API records it
        raise RuntimeError("object store credentials missing")

    monkeypatch.setattr(ingest_mod, "build_deps", broken_deps)
    with pytest.raises(RuntimeError, match="credentials"):
        ingest_flow(cid)
    row = chapter(env, cid)
    assert (row.status, row.error) == ("failed", "ingest could not start")
    assert job_status(env, jobs[0]) == "failed"


def test_cancelled_while_queued_fails_the_pending_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    old = add_job(env, cid, "run-old", status="failed")
    queued = add_job(env, cid, "run-q")
    run = SimpleNamespace(id="run-q", parameters={"chapter_id": cid})
    ingest_mod.on_cancelled(ingest_flow, run, None)  # type: ignore[arg-type]
    row = chapter(env, cid)
    assert (row.status, row.error) == ("failed", "ingest could not start")
    assert (job_status(env, queued), job_status(env, old)) == ("cancelled", "failed")


def test_hook_of_a_superseded_run_leaves_the_pending_chapter_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer source (newer job) was submitted: the old run's crash is not its business."""
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    old = add_job(env, cid, "run-old")
    add_job(env, cid, "run-new")
    run = SimpleNamespace(id="run-old", parameters={"chapter_id": cid})
    ingest_mod.on_crashed(ingest_flow, run, None)  # type: ignore[arg-type]
    row = chapter(env, cid)
    assert (row.status, row.error) == ("pending", None)
    assert job_status(env, old) == "failed"


def test_flow_run_id_is_the_owner_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    seen: list[str | None] = []

    def record(call: int) -> None:
        if call == 1:
            seen.append(chapter(env, cid).ingest_run_id)
            seen.append(prefect_flow_run.id)

    env.ocr.on_call = record
    ingest_flow(cid)
    assert seen[0] is not None and seen[0] == seen[1]
    # direct (non-Prefect) calls get a fresh token each time
    ingest_mod.run_ingest(cid, ingest_mod.build_deps())
    first = chapter(env, cid).ingest_run_id
    ingest_mod.run_ingest(cid, ingest_mod.build_deps())
    assert first and chapter(env, cid).ingest_run_id not in (first, seen[0])


def test_failing_fail_does_not_mask_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    env.ocr.fail_on_call = 1

    def broken_fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("database went away")

    monkeypatch.setattr(ingest_mod, "_fail", broken_fail)
    with pytest.raises(RuntimeError, match="model exploded"):
        ingest_mod.run_ingest(cid, ingest_mod.build_deps())


def test_concurrent_ingest_refused_stale_taken_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    ingest_flow(cid)
    pages_before = count(env, PageRow, cid)

    def set_running(started: datetime) -> None:
        with Session(env.engine) as s, s.begin():
            row = s.get(ChapterRow, cid)
            assert row is not None
            row.status, row.ingest_started_at = "ingesting", started
            row.ingest_heartbeat_at = None  # no beat yet: staleness falls back to the start

    set_running(datetime.now(UTC))
    with pytest.raises(ingest_mod.IngestAlreadyRunning, match="ingest already running"):
        ingest_flow(cid)
    row = chapter(env, cid)
    assert (row.status, row.error) == ("ingesting", None)  # the running ingest is untouched
    assert count(env, PageRow, cid) == pages_before
    assert env.ocr.calls == 3  # only the first run read anything

    set_running(datetime.now(UTC) - timedelta(seconds=STALE_INGEST_S + 60))
    assert ingest_flow(cid).pages == 1
    assert chapter(env, cid).status == "ready"


def test_stale_is_ten_minutes_without_a_heartbeat() -> None:
    assert STALE_INGEST_S == 600


def test_heartbeat_at_start_and_after_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png(), page_png()]))
    beats: list[datetime | None] = []
    env.ocr.on_call = lambda call: beats.append(chapter(env, cid).ingest_heartbeat_at)
    ingest_mod.run_ingest(cid, ingest_mod.build_deps(), run_id="run-a")
    # 3 regions per page: calls 1-3 on page 1, 4-6 on page 2
    assert beats[0] is not None and beats[0] == beats[2]
    assert beats[3] is not None and beats[3] > beats[0]  # beat after page 1
    row = chapter(env, cid)
    assert row.ingest_heartbeat_at is not None and row.ingest_heartbeat_at >= beats[3]


def test_takeover_is_judged_on_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)
    now = datetime.now(UTC)
    stale = timedelta(seconds=STALE_INGEST_S + 60)

    def set_running(started: datetime, beat: datetime | None) -> None:
        with Session(env.engine) as s, s.begin():
            row = s.get(ChapterRow, cid)
            assert row is not None
            row.status, row.ingest_run_id = "ingesting", "run-old"
            row.ingest_started_at, row.ingest_heartbeat_at = started, beat

    set_running(now - timedelta(hours=3), now)  # long run, still beating: refused
    with pytest.raises(ingest_mod.IngestAlreadyRunning):
        ingest_flow(cid)
    assert chapter(env, cid).ingest_run_id == "run-old"
    set_running(now, now - stale)  # recent start but no beat for > STALE: dead, taken over
    assert ingest_flow(cid).pages == 1
    set_running(now - stale, None)  # no beat yet: falls back to the start time
    assert ingest_flow(cid).pages == 1
    row = chapter(env, cid)
    assert row.status == "ready" and row.ingest_run_id != "run-old"


def test_run_that_lost_the_chapter_stops_at_the_next_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png(), page_png()]))

    def take_over(call: int) -> None:
        if call == 1:
            with Session(env.engine) as s, s.begin():
                row = s.get(ChapterRow, cid)
                assert row is not None
                row.ingest_run_id = "run-other"

    env.ocr.on_call = take_over
    with pytest.raises(ingest_mod.IngestAlreadyRunning):
        ingest_mod.run_ingest(cid, ingest_mod.build_deps(), run_id="run-a")
    assert env.ocr.calls == 3  # page 2 was never read
    assert chapter(env, cid).ingest_heartbeat_at is not None


def test_taken_over_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)

    def take_over(call: int) -> None:  # another run takes the chapter mid-way
        if call == 1:
            with Session(env.engine) as s, s.begin():
                row = s.get(ChapterRow, cid)
                assert row is not None
                row.ingest_run_id = "run-other"

    env.ocr.on_call = take_over
    with pytest.raises(ingest_mod.IngestAlreadyRunning):
        ingest_mod.run_ingest(cid, ingest_mod.build_deps())
    row = chapter(env, cid)
    assert row.status == "ingesting"  # left to the run that owns it
    assert count(env, PageRow, cid) == 0


@pytest.mark.parametrize(
    ("result", "field_name", "error_type"),
    [
        (("ok", float("nan")), "ocr_confidence", "less_than_equal"),  # NaN fails the bound
        (("ok", 1.5), "ocr_confidence", "less_than_equal"),
        (("秘" * 3000, 0.95), "text", "string_too_long"),
    ],
)
def test_invalid_ocr_result_becomes_review_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    result: tuple[str, float],
    field_name: str,
    error_type: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("orchestration.ingest_flow", "pipeline.ingest.process", "prefect"):
        caplog.set_level(logging.DEBUG, logger=name)
    env = make_env(tmp_path, monkeypatch)
    env.ocr.results = [result]  # first bubble read (top panel, right balloon) is bad
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)

    summary = ingest_flow(cid)

    assert (summary.pages, summary.bubbles) == (1, 2)
    invalid = [r for r in reviews(env, cid) if r.kind == "bubble_invalid"]
    assert len(invalid) == 1
    assert field_name in invalid[0].reason and error_type in invalid[0].reason
    assert "秘" not in invalid[0].reason and "nan" not in invalid[0].reason.lower()
    assert "ready" in caplog.text  # the capture sees this module's records
    assert "秘秘" not in caplog.text
    with Session(env.engine) as s:
        top = s.scalars(
            select(BubbleRow).where(BubbleRow.chapter_id == cid).order_by(BubbleRow.reading_order)
        ).all()
    assert all(b.artifact["text"] == "テスト" for b in top)
    assert sorted(b.reading_order for b in top) == [1, 1]  # one per panel, renumbered from 1


def test_rights_revoked_mid_run_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, monkeypatch)
    cid = add_chapter(env, make_cbz([page_png()]), page_end=1)

    def revoke(call: int) -> None:
        if call == 1:
            with Session(env.engine) as s, s.begin():
                row = s.scalars(
                    select(RightsRow).where(RightsRow.project_id == env.project_id)
                ).one()
                row.record = {**row.record, "status": "rejected"}

    env.ocr.on_call = revoke
    with pytest.raises(RightsDenied):
        ingest_flow(cid)
    row = chapter(env, cid)
    assert row.status == "failed" and row.error is not None and "rejected" in row.error
    assert count(env, PageRow, cid) == 0
