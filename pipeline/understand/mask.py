"""Flatten the text regions of a panel crop before the vision model sees it.

The scene fields describe what is drawn, so the model has no need of the dialogue. Text that is
merely READ can steer a model whatever its ``evidence`` label says, so the pixels of every
detected text region (bubble boxes, text boxes, free-text boxes) are filled with one flat colour
and the request carries no OCR text either.

``text_mask_boxes`` builds the mask from two independent sources, and uses both:

1. the bubble detector run on the PANEL CROP itself at request time (recall-oriented threshold,
   ``mask_detect_threshold`` in configs/understand.yaml), taking the outer balloon box and the
   text box of every region of every kind (speech, caption, free text);
2. every stored region box of the PAGE that intersects the panel, not only those stored with this
   panel (a balloon that crosses a panel border is stored on one neighbour only).

It does not depend on what ingest managed to store: a region whose OCR result was rejected is
not stored, but the detector still finds it.

REMAINING RISK, stated honestly: this is a mitigation, not a proof. Text the detector misses
(low contrast, unusual lettering, small or rotated text), text drawn as part of the scenery
(signs, posters, sound effects in the artwork) and lettering in scripts the detector was not
trained on stay readable to the model. The impact is limited to a wrong scene description: the
model has no tools and no privileges, its output is validated and cited by code, and a
text-backed claim is refused.
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from typing import Protocol

from PIL import Image, ImageDraw

from pipeline.common.schemas import BBox
from pipeline.common.uploads import open_image_checked
from pipeline.ingest.bubbles import Region

__all__ = ["Detector", "mask_panel", "mask_text_regions", "text_mask_boxes"]

_PNG = ("PNG",)


def _border_median(img: Image.Image) -> tuple[int, int, int]:
    w, h = img.size
    coords = [(x, y) for x in range(w) for y in (0, h - 1)]
    coords += [(x, y) for y in range(1, h - 1) for x in (0, w - 1)]
    border = [img.getpixel(c) for c in coords]
    channels = [sorted(p[i] for p in border if isinstance(p, tuple)) for i in range(3)]
    return (
        channels[0][len(channels[0]) // 2],
        channels[1][len(channels[1]) // 2],
        channels[2][len(channels[2]) // 2],
    )


def mask_text_regions(png: bytes, boxes: Sequence[BBox], panel_bbox: BBox) -> bytes:
    """The crop with each box (PAGE coordinates) filled flat; boxes outside it are ignored.

    ``panel_bbox`` is where the crop sits on the page. A crop whose size differs from the panel
    box scales the boxes with it. The fill is the median colour of the crop's border. With no
    box inside the crop the pixels come back unchanged.
    """
    with open_image_checked(png, _PNG) as src:
        img = src.convert("RGB")
    w, h = img.size
    sx, sy = w / panel_bbox.w, h / panel_bbox.h
    rects: list[tuple[int, int, int, int]] = []
    for b in boxes:
        if b.w <= 0 or b.h <= 0:
            continue
        x0 = max(0, int((b.x - panel_bbox.x) * sx))
        y0 = max(0, int((b.y - panel_bbox.y) * sy))
        x1 = min(w, math.ceil((b.x - panel_bbox.x + b.w) * sx))
        y1 = min(h, math.ceil((b.y - panel_bbox.y + b.h) * sy))
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1 - 1, y1 - 1))  # ImageDraw rectangles are inclusive
    if rects:
        fill = _border_median(img)
        draw = ImageDraw.Draw(img)
        for r in rects:
            draw.rectangle(r, fill=fill)
    clean = Image.new("RGB", img.size)  # fresh image: no ICC profile, text chunks or metadata
    clean.paste(img)
    out = io.BytesIO()
    clean.save(out, format="PNG")
    return out.getvalue()


class Detector(Protocol):
    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]: ...


def text_mask_boxes(
    crop_png: bytes,
    panel_bbox: BBox,
    stored_boxes: Sequence[BBox],
    detector: Detector,
    threshold: float = 0.35,
) -> list[BBox]:
    """Every box to blank, in CROP coordinates and clamped to the crop.

    ``stored_boxes`` are region boxes of the whole page (page coordinates); those that intersect
    the panel count. The detector runs on the crop itself.
    """
    with open_image_checked(crop_png, _PNG) as src:
        crop = src.convert("RGB")
    w, h = crop.size
    sx, sy = w / panel_bbox.w, h / panel_bbox.h
    out: list[BBox] = []

    def add(x0: float, y0: float, x1: float, y1: float) -> None:
        cx0, cy0 = max(0, math.floor(x0)), max(0, math.floor(y0))
        cx1, cy1 = min(w, math.ceil(x1)), min(h, math.ceil(y1))
        if cx1 > cx0 and cy1 > cy0:
            out.append(BBox(x=cx0, y=cy0, w=cx1 - cx0, h=cy1 - cy0))

    for b in stored_boxes:
        if b.w <= 0 or b.h <= 0:
            continue
        add(
            (b.x - panel_bbox.x) * sx,
            (b.y - panel_bbox.y) * sy,
            (b.x - panel_bbox.x + b.w) * sx,
            (b.y - panel_bbox.y + b.h) * sy,
        )
    for r in detector.detect(crop, threshold):
        for box in (r.bbox, r.text_bbox):
            if box is not None:
                add(box.x, box.y, box.x + box.w, box.y + box.h)
    return out


def mask_panel(
    crop_png: bytes,
    panel_bbox: BBox,
    stored_boxes: Sequence[BBox],
    detector: Detector,
    threshold: float = 0.35,
) -> bytes:
    """The crop PNG with every text region (see ``text_mask_boxes``) filled flat."""
    boxes = text_mask_boxes(crop_png, panel_bbox, stored_boxes, detector, threshold)
    with open_image_checked(crop_png, _PNG) as src:
        size = src.size
    return mask_text_regions(crop_png, boxes, BBox(x=0, y=0, w=size[0], h=size[1]))
