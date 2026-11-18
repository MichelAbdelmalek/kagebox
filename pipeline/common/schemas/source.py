"""Source material: projects, chapters, pages, panels and speech bubbles."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, computed_field, model_validator

from pipeline.common.ids import (
    BubbleId,
    ChapterId,
    CharacterId,
    ObjectRef,
    PageId,
    PanelId,
    ProjectId,
    RightsId,
)
from pipeline.common.schemas.base import Artifact, Cited, Language, Score, Sha256, ShortText, Strict

# Kept in sync with configs/thresholds.yaml (a unit test checks this).
OCR_CONFIDENCE_MIN = 0.90

Format = Literal["manga_rtl", "comic_ltr", "manhwa_vertical"]


class Project(Strict):
    id: ProjectId
    title: ShortText
    format: Format
    rights_id: RightsId | None = None


class Chapter(Strict):
    id: ChapterId
    project_id: ProjectId
    number: Annotated[int, Field(ge=0, le=100_000)]
    title: ShortText | None = None
    source_hash: Sha256


class BBox(Strict):
    """Pixel box on the page image: x, y = top-left."""

    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    w: Annotated[int, Field(gt=0)]
    h: Annotated[int, Field(gt=0)]


class Page(Artifact):
    id: PageId
    chapter_id: ChapterId
    index: Annotated[int, Field(ge=0)]
    width: Annotated[int, Field(gt=0, le=60_000)]
    height: Annotated[int, Field(gt=0, le=200_000)]  # manhwa strips are tall
    image_ref: ObjectRef


class Panel(Artifact):
    id: PanelId
    page_id: PageId
    bbox: BBox
    polygon: list[tuple[int, int]] | None = Field(default=None, max_length=256)
    reading_order: Annotated[int, Field(ge=1)]
    crop_ref: ObjectRef


class Bubble(Artifact):
    id: BubbleId
    panel_id: PanelId
    bbox: BBox
    kind: Literal["speech", "thought", "caption", "sfx"]
    text: Annotated[str, StringConstraints(max_length=2000)]  # untrusted OCR output
    lang: Language
    ocr_confidence: Score
    ocr_engine: ShortText
    speaker: Cited[CharacterId]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_review(self) -> bool:
        return self.ocr_confidence < OCR_CONFIDENCE_MIN

    @model_validator(mode="after")
    def _speaker_cites_own_panel(self) -> Bubble:
        if self.speaker.known and self.panel_id not in self.speaker.panel_ids:
            raise ValueError("speaker attribution must cite the bubble's own panel")
        return self
