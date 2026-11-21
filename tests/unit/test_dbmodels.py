from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
    SceneRow,
    StageRunRow,
    User,
    aware,
    claim_stage,
    stage_alive,
    stage_takeover_allowed,
)
from pipeline.common.ids import new_id
from pipeline.common.schemas import Bubble, Page, Panel
from pipeline.common.settings import Settings

NOW = datetime(2026, 11, 19, tzinfo=UTC)
REF = "media/sha256/" + "a" * 64 + ".png"
PROV: dict[str, Any] = {
    "stage": "ingest",
    "tool": "test",
    "tool_version": "0",
    "license": "Apache-2.0",
    "provider": "none",
    "params_hash": "0" * 64,
    "created_at": NOW,
}


def _session(tmp_path: Any) -> Session:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _seed(s: Session) -> dict[str, str]:
    ids = {k: new_id(k) for k in ("user", "proj", "chap", "page", "panel", "bubble")}
    s.add(User(id=ids["user"], email="a@b.c", password_hash="x"))
    s.flush()
    s.add(ProjectRow(id=ids["proj"], title="T", format="manga_rtl", created_by=ids["user"]))
    s.flush()
    s.add(
        ChapterRow(
            id=ids["chap"],
            project_id=ids["proj"],
            number=1,
            title=None,
            language="ja",
            format="manga_rtl",
            source_kind="upload",
            source_ref=REF,
            source_sha256="a" * 64,
            page_start=1,
            page_end=1,
            status="pending",
            error=None,
        )
    )
    s.flush()
    page = Page.model_validate(
        {
            "id": ids["page"],
            "chapter_id": ids["chap"],
            "index": 1,
            "source_page": 1,
            "width": 10,
            "height": 20,
            "image_ref": REF,
            "provenance": PROV,
        }
    )
    s.add(
        PageRow(
            id=ids["page"], chapter_id=ids["chap"], index=1, artifact=page.model_dump(mode="json")
        )
    )
    s.flush()
    panel = Panel.model_validate(
        {
            "id": ids["panel"],
            "page_id": ids["page"],
            "bbox": {"x": 0, "y": 0, "w": 5, "h": 5},
            "reading_order": 1,
            "crop_ref": REF,
            "provenance": PROV,
        }
    )
    s.add(
        PanelRow(
            id=ids["panel"],
            page_id=ids["page"],
            chapter_id=ids["chap"],
            reading_order=1,
            artifact=panel.model_dump(mode="json"),
        )
    )
    s.flush()
    bubble = Bubble.model_validate(
        {
            "id": ids["bubble"],
            "panel_id": ids["panel"],
            "bbox": {"x": 1, "y": 1, "w": 2, "h": 2},
            "kind": "speech",
            "text": "hi",
            "lang": "ja",
            "ocr_confidence": 0.5,
            "ocr_engine": "x",
            "speaker": {},
            "reading_order": 1,
            "provenance": PROV,
        }
    )
    s.add(
        BubbleRow(
            id=ids["bubble"],
            panel_id=ids["panel"],
            chapter_id=ids["chap"],
            reading_order=1,
            needs_review=bubble.needs_review,
            artifact=bubble.model_dump(mode="json", exclude={"needs_review"}),
        )
    )
    s.add(
        ReviewItemRow(
            id=new_id("rev"),
            project_id=ids["proj"],
            chapter_id=ids["chap"],
            artifact_id=ids["page"],
            kind="region_outside_panels",
            reason="r",
            status="open",
        )
    )
    s.add(JobRow(id=new_id("job"), chapter_id=ids["chap"], flow_run_id=None, status="pending"))
    s.commit()
    return ids


def test_ingest_tables_roundtrip(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    assert s.get(ChapterRow, ids["chap"]).status == "pending"  # type: ignore[union-attr]
    assert s.get(BubbleRow, ids["bubble"]).needs_review is True  # type: ignore[union-attr]
    assert s.scalars(select(ReviewItemRow)).one().status == "open"
    s.delete(s.get(ChapterRow, ids["chap"]))
    s.commit()
    for model in (PageRow, PanelRow, BubbleRow, JobRow, ReviewItemRow):
        assert s.scalars(select(model)).all() == []


def test_artifact_json_validates(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    Page.model_validate_json(json.dumps(s.get(PageRow, ids["page"]).artifact))  # type: ignore[union-attr]
    Panel.model_validate_json(json.dumps(s.get(PanelRow, ids["panel"]).artifact))  # type: ignore[union-attr]
    Bubble.model_validate_json(json.dumps(s.get(BubbleRow, ids["bubble"]).artifact))  # type: ignore[union-attr]


def test_bubble_reading_order_unique_per_panel(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    row = s.get(BubbleRow, ids["bubble"])
    assert row is not None
    s.add(
        BubbleRow(
            id=new_id("bubble"),
            panel_id=ids["panel"],
            chapter_id=ids["chap"],
            reading_order=1,  # same panel, same position
            artifact=row.artifact,
        )
    )
    with pytest.raises(IntegrityError):
        s.commit()


def test_chapter_ingest_started_at_nullable(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    chapter = s.get(ChapterRow, ids["chap"])
    assert chapter is not None
    assert (chapter.ingest_started_at, chapter.ingest_run_id) == (None, None)
    chapter.ingest_started_at, chapter.ingest_run_id = NOW, "f3c1e2d4-run"
    s.commit()
    assert aware(chapter.ingest_started_at) == NOW
    assert chapter.ingest_run_id == "f3c1e2d4-run"


def _scene(panel_id: str, chapter_id: str, sid: str | None = None) -> SceneRow:
    return SceneRow(
        id=sid or "scene_" + new_id("panel"),
        panel_id=panel_id,
        chapter_id=chapter_id,
        params_hash="b" * 64,
        artifact={"k": "v"},
    )


def test_scene_unique_per_panel_and_cascades_with_panel_and_chapter(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    s.add(_scene(ids["panel"], ids["chap"]))
    s.commit()
    s.add(_scene(ids["panel"], ids["chap"]))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.execute(delete(PanelRow).where(PanelRow.id == ids["panel"]))
    s.commit()
    assert s.scalars(select(SceneRow)).all() == []
    # chapter cascade, on a fresh database
    (tmp_path / "b").mkdir()
    s = _session(tmp_path / "b")
    other = _seed(s)
    s.add(_scene(other["panel"], other["chap"]))
    s.commit()
    s.execute(delete(ChapterRow).where(ChapterRow.id == other["chap"]))
    s.commit()
    assert s.scalars(select(SceneRow)).all() == []


def test_claim_stage_creates_missing_row(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    assert claim_stage(s, ids["chap"], "understand", "run-a", NOW) is True
    s.commit()
    row = s.get(StageRunRow, (ids["chap"], "understand"))
    assert row is not None
    assert (row.status, row.run_id) == ("running", "run-a")
    assert aware(row.started_at) == NOW and aware(row.heartbeat_at) == NOW  # type: ignore[arg-type]


def test_claim_stage_refuses_live_and_takes_over_stale(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    assert claim_stage(s, ids["chap"], "understand", "run-a", NOW)
    s.commit()
    soon = NOW + timedelta(seconds=60)
    assert claim_stage(s, ids["chap"], "understand", "run-b", soon) is False
    s.commit()
    row = s.get(StageRunRow, (ids["chap"], "understand"))
    assert row is not None
    s.refresh(row)
    assert row.run_id == "run-a"
    # another stage is independent
    assert claim_stage(s, ids["chap"], "bible", "run-b", soon) is True
    s.commit()
    late = NOW + timedelta(seconds=STALE_INGEST_S + 1)
    assert claim_stage(s, ids["chap"], "understand", "run-b", late) is True
    s.commit()
    s.refresh(row)
    assert (row.run_id, row.status) == ("run-b", "running")
    # a finished stage can be claimed again at once
    row.status = "done"
    s.commit()
    assert claim_stage(s, ids["chap"], "understand", "run-c", soon) is True
    s.commit()
    s.refresh(row)
    assert (row.run_id, row.status, row.finished_at, row.error) == ("run-c", "running", None, None)


def test_stage_alive_matches_sql_rule(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    stale_before = NOW - timedelta(seconds=STALE_INGEST_S)
    cases = [
        ("running", NOW, None),  # fresh heartbeat
        ("running", stale_before - timedelta(seconds=1), None),  # stale heartbeat
        ("running", stale_before - timedelta(seconds=1), NOW),  # stale start, fresh beat
        ("running", None, NOW),  # no beat yet, fresh start
        ("running", None, stale_before - timedelta(seconds=1)),  # no beat, stale start
        ("running", None, None),  # nothing at all
        ("done", NOW, NOW),
        ("failed", NOW, NOW),
        ("pending", NOW, NOW),
    ]
    for i, (status, beat, started) in enumerate(cases):
        s.add(
            StageRunRow(
                chapter_id=ids["chap"],
                stage=f"s{i}",
                status=status,
                run_id="r",
                heartbeat_at=beat,
                started_at=started,
            )
        )
    s.commit()
    for row in s.scalars(select(StageRunRow)):
        allowed = s.scalar(
            select(StageRunRow.stage).where(
                StageRunRow.stage == row.stage, stage_takeover_allowed(stale_before)
            )
        )
        assert stage_alive(row, stale_before) == (allowed is None), row.stage


def test_job_stage_defaults_to_ingest(tmp_path: Any) -> None:
    s = _session(tmp_path)
    ids = _seed(s)
    job = s.scalars(select(JobRow)).one()
    assert job.stage == "ingest"
    s.execute(
        text(
            "INSERT INTO jobs (id, chapter_id, status, created_at, updated_at) "
            "VALUES ('j2', :c, 'submitted', :t, :t)"
        ),
        {"c": ids["chap"], "t": NOW},
    )
    s.commit()
    assert s.get(JobRow, "j2").stage == "ingest"  # type: ignore[union-attr]
