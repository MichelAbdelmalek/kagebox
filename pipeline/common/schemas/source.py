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
    """One page as read. ``index`` is the 1-based reading position within the chapter.

    ``source_page`` is the 1-based page (or strip image) number in the source file. A manhwa
    strip is cut into several Pages; each records its rows ``[source_y0, source_y1)`` of the
    source image (both set for a slice, both null for a whole page).
    """

    id: PageId
    chapter_id: ChapterId
    index: Annotated[int, Field(ge=1)]
    source_page: Annotated[int, Field(ge=1)]
    source_y0: Annotated[int, Field(ge=0)] | None = None
    source_y1: Annotated[int, Field(gt=0)] | None = None
    width: Annotated[int, Field(gt=0, le=60_000)]
    height: Annotated[int, Field(gt=0, le=200_000)]  # manhwa strips are tall
    image_ref: ObjectRef

    @model_validator(mode="after")
    def _slice_range(self) -> Page:
        if (self.source_y0 is None) != (self.source_y1 is None):
            raise ValueError("source_y0 and source_y1 are set together or not at all")
        y0, y1 = self.source_y0, self.source_y1
        if y0 is not None and y1 is not None and y1 <= y0:
            raise ValueError("source_y1 must be greater than source_y0")
        return self


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
    reading_order: Annotated[int, Field(ge=1)]  # 1-based, within its panel

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_review(self) -> bool:
        return self.ocr_confidence < OCR_CONFIDENCE_MIN

    @model_validator(mode="after")
    def _speaker_cites_own_panel(self) -> Bubble:
        if self.speaker.known and self.panel_id not in self.speaker.panel_ids:
            raise ValueError("speaker attribution must cite the bubble's own panel")
        return self
