"""Real pinned manga-bubble-detector on a synthetic page with drawn speech balloons."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from pipeline.common.schemas import BBox
from pipeline.ingest.bubbles import BubbleDetector

pytestmark = pytest.mark.models

BALLOONS = [BBox(x=80, y=80, w=300, h=200), BBox(x=430, y=500, w=300, h=200)]


def synthetic_page() -> Image.Image:
    img = Image.new("RGB", (800, 1100), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 780, 1080), outline="black", width=6)
    for b in BALLOONS:
        x0, y0 = b.x, b.y
        d.ellipse((x0, y0, x0 + b.w, y0 + b.h), fill="white", outline="black", width=4)
        d.polygon([(x0 + 140, y0 + 195), (x0 + 120, y0 + 270), (x0 + 175, y0 + 195)], fill="black")
        for i in range(4):
            d.line((x0 + 70, y0 + 60 + 25 * i, x0 + 230, y0 + 60 + 25 * i), fill="black", width=8)
    return img


def iou(a: BBox, b: BBox) -> float:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    inter = w * h if w > 0 and h > 0 else 0
    return inter / (a.w * a.h + b.w * b.h - inter)


def test_one_speech_region_per_drawn_balloon() -> None:
    img = synthetic_page()
    regions = BubbleDetector().detect(img)  # default threshold
    detail = [(r.label, r.bbox, r.text_bbox, round(r.score, 3)) for r in regions]
    for balloon in BALLOONS:
        hits = [r for r in regions if r.kind == "speech" and iou(r.bbox, balloon) >= 0.5]
        assert len(hits) == 1, (balloon, detail)
    for r in regions:  # nothing outside the drawn balloons
        assert any(iou(r.bbox, b) >= 0.5 for b in BALLOONS), (r, detail)
        assert r.bbox.x + r.bbox.w <= img.width and r.bbox.y + r.bbox.h <= img.height
