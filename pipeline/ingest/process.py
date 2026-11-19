"""One page image in, Page / Panel / Bubble artifacts and review notes out (no DB, no Prefect).

Per page: the image and every panel crop go to the ``media`` bucket (content-addressed, so a
re-run stores nothing twice); panels come from ``find_panels`` in XY-cut reading order; regions
from the bubble detector are assigned to panels and ordered inside each panel; OCR reads
``Region.text_bbox`` when present, else ``Region.bbox``.

Bubble ids are generated per panel, sorted, then handed out in reading order, so ordering a
panel's bubbles by id gives their reading order (Bubble has no order field).

Nothing fails silently. Review notes:

* ``panel_full_page``: a whole-page panel (``full_page`` or ``no_components``), to confirm;
* ``panel_irregular``: a component panel whose fill is below ``panel_irregular_fill``;
* ``region_outside_panels``: a region overlapping no panel; it never becomes a Bubble (R1);
* ``ocr_low_confidence``: OCR confidence below ``OCR_CONFIDENCE_MIN``;
* ``page_no_text``: the page had regions to read and every OCR result was empty.

OCR text is untrusted data: it is stored as a string and never put into a reason, path or query.
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

from pipeline.common.ids import new_id
from pipeline.common.schemas import BBox, Bubble, Page, Panel, Provenance
from pipeline.common.schemas.source import OCR_CONFIDENCE_MIN
from pipeline.common.storage import ObjectStore
from pipeline.ingest.bubbles import Region, assign, order_in_panel
from pipeline.ingest.panels import Direction, PanelCandidate, find_panels, reading_order

ReviewKind = Literal[
    "panel_full_page",
    "panel_irregular",
    "region_outside_panels",
    "ocr_low_confidence",
    "page_no_text",
]
REVIEW_KINDS: tuple[ReviewKind, ...] = (
    "panel_full_page",
    "panel_irregular",
    "region_outside_panels",
    "ocr_low_confidence",
    "page_no_text",
)


class Detector(Protocol):
    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]: ...


class Ocr(Protocol):
    @property
    def engine(self) -> str: ...

    def read(self, crop: Image.Image) -> tuple[str, float]: ...


@dataclass(frozen=True)
class ModelInfo:
    """Provenance facts for one tool: a pinned model from models.yaml, or Kagebox itself."""

    name: str
    version: str  # pinned revision for models, package version for Kagebox
    license: str
    source_url: str | None
    is_model: bool = True


@dataclass(frozen=True)
class PageContext:
    """Everything that is the same for every page of one ingest run."""

    chapter_id: str
    language: str
    direction: Direction
    source_sha256: str
    store: ObjectStore
    detector: Detector
    ocr: Ocr
    kagebox: ModelInfo
    detector_info: ModelInfo
    ocr_info: ModelInfo
    detect_threshold: float
    irregular_fill: float
    now: datetime


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


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _box(b: BBox) -> tuple[int, int, int, int]:
    return (b.x, b.y, b.x + b.w, b.y + b.h)


def _where(b: BBox) -> str:
    return f"x={b.x} y={b.y} w={b.w} h={b.h}"


def provenance(
    ctx: PageContext, tool: ModelInfo, params: dict[str, Any], inputs: Sequence[str]
) -> Provenance:
    blob = json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    return Provenance(
        stage="ingest",
        tool=tool.name,
        tool_version=tool.version,
        model_revision=tool.version if tool.is_model else None,
        license=tool.license,
        source_url=tool.source_url,
        provider="local",
        params_hash=_sha(blob),
        input_hashes=list(inputs),
        created_at=ctx.now,
    )


def process_page(ctx: PageContext, index: int, img: Image.Image) -> PageResult:
    img = img.convert("RGB")
    page_png = _png(img)
    page_hash = _sha(page_png)
    geometry = {"tool": ctx.kagebox.name, "direction": ctx.direction}
    page = Page(
        id=new_id("page"),
        chapter_id=ctx.chapter_id,
        index=index,
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
        "ocr": ctx.ocr_info.name,
        "ocr_revision": ctx.ocr_info.version,
        "direction": ctx.direction,
        "language": ctx.language,
    }
    bubble_prov = provenance(ctx, ctx.ocr_info, bubble_params, [ctx.source_sha256, page_hash])
    texts: list[str] = []
    for i, panel in ordered:
        for bubble in _bubbles(ctx, panel, per_panel[i], img, bubble_prov):
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
    if cand.fill < ctx.irregular_fill:
        reason = (
            f"panel at {_where(cand.bbox)} fills {cand.fill:.2f} of its box "
            f"(below {ctx.irregular_fill:.2f}); panels may be merged"
        )
        return [ReviewNote(panel.id, "panel_irregular", reason)]
    return []


def _bubbles(
    ctx: PageContext, panel: Panel, regions: list[Region], img: Image.Image, prov: Provenance
) -> list[Bubble]:
    if not regions:
        return []
    ids = sorted(new_id("bubble") for _ in regions)  # id order = reading order
    out: list[Bubble] = []
    for bubble_id, j in zip(ids, order_in_panel(regions, ctx.direction), strict=True):
        region = regions[j]
        crop = img.crop(_box(region.text_bbox or region.bbox))
        text, confidence = ctx.ocr.read(crop)
        out.append(
            Bubble.model_validate(
                {
                    "id": bubble_id,
                    "panel_id": panel.id,
                    "bbox": region.bbox,
                    "kind": region.kind,
                    "text": text,
                    "lang": ctx.language,
                    "ocr_confidence": confidence,
                    "ocr_engine": ctx.ocr.engine,
                    "speaker": {},  # speaker linking is milestone 1.3: no evidence yet
                    "provenance": prov,
                }
            )
        )
    return out
