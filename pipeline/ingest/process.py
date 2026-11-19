"""One page image in, Page / Panel / Bubble artifacts and review notes out (no DB, no Prefect).

Per page: the image and every panel crop go to the ``media`` bucket (content-addressed, so a
re-run stores nothing twice); panels come from ``find_panels`` in XY-cut reading order; regions
from the bubble detector are assigned to panels and ordered inside each panel
(``Bubble.reading_order``, 1-based); OCR reads ``Region.text_bbox`` when present, else
``Region.bbox``.

Nothing fails silently. Review notes:

* ``panel_full_page``: a whole-page panel (``full_page`` or ``no_components``), to confirm;
* ``panel_irregular``: a component panel whose fill is below ``panel_irregular_fill``;
* ``panel_merged``: a panel unioned from overlapping components (ruling P4), always, whatever
  its fill: pieces of different panels can fill their union well;
* ``region_outside_panels``: a region overlapping no panel; it never becomes a Bubble (R1);
* ``ocr_low_confidence``: OCR confidence below ``OCR_CONFIDENCE_MIN``;
* ``page_no_text``: the page had regions to read and every OCR result was empty;
* ``bubble_invalid``: an OCR result that fails the Bubble schema (e.g. NaN confidence, text
  over the length limit); that bubble is skipped and the rest of the page goes on.

OCR text is untrusted data: it is stored as a string and never put into a reason, a log line,
a path or a query. Validation errors are reported by field name and error type only.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import numpy as np
from PIL import Image
from pydantic import ValidationError

from pipeline.common.ids import new_id
from pipeline.common.schemas import BBox, Bubble, ModelRef, Page, Panel, Provenance
from pipeline.common.schemas.source import OCR_CONFIDENCE_MIN
from pipeline.common.storage import ObjectStore
from pipeline.ingest.bubbles import Region, assign, order_in_panel
from pipeline.ingest.panels import Direction, PanelCandidate, find_panels, reading_order
from pipeline.ingest.sources import Fit

ReviewKind = Literal[
    "panel_full_page",
    "panel_irregular",
    "panel_merged",
    "region_outside_panels",
    "ocr_low_confidence",
    "page_no_text",
    "bubble_invalid",
    "slice_hard_cut",
]
REVIEW_KINDS: tuple[ReviewKind, ...] = (
    "panel_full_page",
    "panel_irregular",
    "panel_merged",
    "region_outside_panels",
    "ocr_low_confidence",
    "page_no_text",
    "bubble_invalid",
    "slice_hard_cut",
)


class Detector(Protocol):
    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]: ...


class Ocr(Protocol):
    @property
    def engine(self) -> str: ...  # the recognition model: Bubble.ocr_engine

    @property
    def models(self) -> tuple[str, ...]: ...  # every pinned model it runs

    def read(self, crop: Image.Image) -> tuple[str, float]: ...


@dataclass(frozen=True)
class ModelInfo:
    """Provenance facts for one tool: a pinned model from models.yaml, or Kagebox itself."""

    name: str
    version: str  # pinned revision for models, package version for Kagebox
    license: str
    source_url: str | None
    is_model: bool = True

    def ref(self) -> ModelRef:
        return ModelRef(
            name=self.name, revision=self.version, license=self.license, source_url=self.source_url
        )


@dataclass(frozen=True)
class PageContext:
    """Everything that is the same for every page of one ingest run."""

    chapter_id: str
    language: str
    direction: Direction
    fit: Fit
    source_sha256: str
    store: ObjectStore
    detector: Detector
    ocr: Ocr
    kagebox: ModelInfo
    detector_info: ModelInfo
    ocr_info: ModelInfo  # the recognition model (Provenance.tool of a Bubble)
    components: tuple[ModelInfo, ...]  # detector, then every OCR model
    detect_threshold: float
    irregular_fill: float
    now: datetime


@dataclass(frozen=True)
class SourcePosition:
    """Where a Page came from: the source page number and, for a strip slice, its rows."""

    page: int
    y0: int | None = None
    y1: int | None = None


@dataclass(frozen=True)
class ReviewNote:
    artifact_id: str
    kind: ReviewKind
    reason: str


@dataclass
class PageResult:
    page: Page
    panels: list[Panel] = field(default_factory=list)
    bubbles: list[Bubble] = field(default_factory=list)
    reviews: list[ReviewNote] = field(default_factory=list)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def params_hash(params: dict[str, Any]) -> str:
    return _sha(json.dumps(params, sort_keys=True, separators=(",", ":")).encode())


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _box(b: BBox) -> tuple[int, int, int, int]:
    return (b.x, b.y, b.x + b.w, b.y + b.h)


def _where(b: BBox) -> str:
    return f"x={b.x} y={b.y} w={b.w} h={b.h}"


def safe_errors(exc: ValidationError) -> str:
    """Field names and error types only: never the (untrusted) input values."""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['type']}" for e in errors
    )


def provenance(
    ctx: PageContext,
    tool: ModelInfo,
    params: dict[str, Any],
    inputs: Sequence[str],
    components: Sequence[ModelInfo] = (),
) -> Provenance:
    return Provenance(
        stage="ingest",
        tool=tool.name,
        tool_version=tool.version,
        model_revision=tool.version if tool.is_model else None,
        license=tool.license,
        source_url=tool.source_url,
        provider="local",
        params_hash=params_hash(params),
        input_hashes=list(inputs),
        components=[c.ref() for c in components],
        created_at=ctx.now,
    )


def process_page(
    ctx: PageContext, index: int, source: SourcePosition, img: Image.Image
) -> PageResult:
    """``index`` is the 1-based reading position of this Page within the chapter."""
    img = img.convert("RGB")
    page_png = _png(img)
    page_hash = _sha(page_png)
    geometry = {"tool": ctx.kagebox.name, "direction": ctx.direction, "fit": ctx.fit}
    page = Page(
        id=new_id("page"),
        chapter_id=ctx.chapter_id,
        index=index,
        source_page=source.page,
        source_y0=source.y0,
        source_y1=source.y1,
        width=img.width,
        height=img.height,
        image_ref=ctx.store.put("media", page_png, "png", "image/png"),
        provenance=provenance(ctx, ctx.kagebox, geometry, [ctx.source_sha256]),
    )
    result = PageResult(page)

    cands = find_panels(np.asarray(img))
    panel_params = {**geometry, "panel_irregular_fill": ctx.irregular_fill}
    panel_prov = provenance(ctx, ctx.kagebox, panel_params, [ctx.source_sha256, page_hash])
    ordered: list[tuple[int, Panel]] = []  # (candidate index, panel) in reading order
    for rank, i in enumerate(reading_order([c.bbox for c in cands], ctx.direction), 1):
        panel = _panel(ctx, page, cands[i], rank, img, panel_prov)
        ordered.append((i, panel))
        result.panels.append(panel)
        result.reviews.extend(_panel_notes(ctx, panel, cands[i]))

    regions = ctx.detector.detect(img, ctx.detect_threshold)
    per_panel: dict[int, list[Region]] = {i: [] for i, _ in ordered}
    for region, where in assign(regions, [c.bbox for c in cands]):
        if where is None:
            reason = f"{region.kind} region at {_where(region.bbox)} overlaps no panel"
            result.reviews.append(ReviewNote(page.id, "region_outside_panels", reason))
        else:
            per_panel[where].append(region)

    bubble_params = {
        "detector": ctx.detector_info.name,
        "detector_revision": ctx.detector_info.version,
        "detect_threshold": ctx.detect_threshold,
        "ocr": [c.name for c in ctx.components[1:]],
        "direction": ctx.direction,
        "fit": ctx.fit,
        "language": ctx.language,
    }
    bubble_prov = provenance(
        ctx, ctx.ocr_info, bubble_params, [ctx.source_sha256, page_hash], ctx.components
    )
    texts: list[str] = []
    for i, panel in ordered:
        regs = per_panel[i]
        for j in order_in_panel(regs, ctx.direction) if regs else []:
            bubble = _bubble(ctx, panel, regs[j], img, bubble_prov, result)
            if bubble is None:
                continue
            texts.append(bubble.text)
            result.bubbles.append(bubble)
            if bubble.needs_review:
                reason = (
                    f"OCR confidence {bubble.ocr_confidence:.2f} is below {OCR_CONFIDENCE_MIN:.2f}"
                )
                result.reviews.append(ReviewNote(bubble.id, "ocr_low_confidence", reason))
    if texts and not any(t.strip() for t in texts):
        reason = f"{len(texts)} text region(s) found but OCR read no text"
        result.reviews.append(ReviewNote(page.id, "page_no_text", reason))
    return result


def _panel(
    ctx: PageContext,
    page: Page,
    cand: PanelCandidate,
    rank: int,
    img: Image.Image,
    prov: Provenance,
) -> Panel:
    crop = _png(img.crop(_box(cand.bbox)))
    return Panel(
        id=new_id("panel"),
        page_id=page.id,
        bbox=cand.bbox,
        reading_order=rank,
        crop_ref=ctx.store.put("media", crop, "png", "image/png"),
        provenance=prov,
    )


def _panel_notes(ctx: PageContext, panel: Panel, cand: PanelCandidate) -> list[ReviewNote]:
    if cand.reason != "component":
        reason = f"single whole-page panel ({cand.reason}); confirm the page has one panel"
        return [ReviewNote(panel.id, "panel_full_page", reason)]
    notes: list[ReviewNote] = []
    if cand.merged:
        reason = (
            f"panel at {_where(cand.bbox)} merged from {cand.pieces} components with "
            f"overlapping boxes; fills {cand.fill:.2f} of its box; confirm it is one panel"
        )
        notes.append(ReviewNote(panel.id, "panel_merged", reason))
    if cand.fill < ctx.irregular_fill:
        reason = (
            f"panel at {_where(cand.bbox)} fills {cand.fill:.2f} of its box "
            f"(below {ctx.irregular_fill:.2f}); panels may be merged"
        )
        notes.append(ReviewNote(panel.id, "panel_irregular", reason))
    return notes


def _bubble(
    ctx: PageContext,
    panel: Panel,
    region: Region,
    img: Image.Image,
    prov: Provenance,
    result: PageResult,
) -> Bubble | None:
    """OCR one region into a Bubble; a result that fails validation becomes a review note."""
    text, confidence = ctx.ocr.read(img.crop(_box(region.text_bbox or region.bbox)))
    rank = 1 + sum(b.panel_id == panel.id for b in result.bubbles)
    try:
        return Bubble.model_validate(
            {
                "id": new_id("bubble"),
                "panel_id": panel.id,
                "bbox": region.bbox,
                "kind": region.kind,
                "text": text,
                "lang": ctx.language,
                "ocr_confidence": confidence,
                "ocr_engine": ctx.ocr.engine,
                "speaker": {},  # speaker linking is milestone 1.3: no evidence yet
                "reading_order": rank,
                "provenance": prov,
            }
        )
    except ValidationError as exc:
        reason = f"OCR result for region at {_where(region.bbox)} rejected: {safe_errors(exc)}"
        result.reviews.append(ReviewNote(result.page.id, "bubble_invalid", reason))
        return None
