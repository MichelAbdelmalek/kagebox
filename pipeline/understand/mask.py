"""Flatten detected text regions of a panel crop before the vision model sees it.

The scene fields describe what is drawn, so the model has no need of the dialogue. Text that is
merely READ can steer a model whatever its ``evidence`` label says, so the pixels of every
detected text region (bubble boxes, text boxes, free-text boxes) are filled with one flat colour
and the request carries no OCR text either.
"""

from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image, ImageDraw

from pipeline.common.schemas import BBox
from pipeline.common.uploads import open_image_checked

__all__ = ["mask_text_regions"]

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
        x1 = min(w, int((b.x - panel_bbox.x + b.w) * sx + 0.999))
        y1 = min(h, int((b.y - panel_bbox.y + b.h) * sy + 0.999))
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1 - 1, y1 - 1))  # ImageDraw rectangles are inclusive
    if rects:
        fill = _border_median(img)
        draw = ImageDraw.Draw(img)
        for r in rects:
            draw.rectangle(r, fill=fill)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
