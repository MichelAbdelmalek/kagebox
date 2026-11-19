from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    User,
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
            "provenance": PROV,
        }
    )
    s.add(
        BubbleRow(
            id=ids["bubble"],
            panel_id=ids["panel"],
            chapter_id=ids["chap"],
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
