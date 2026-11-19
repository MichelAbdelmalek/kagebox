"""Real pinned manga-bubble-detector on a synthetic page with drawn speech balloons."""

from __future__ import annotations

from PIL import Image, ImageDraw
import pytest

from pipeline.ingest.bubbles import BubbleDetector

pytestmark = pytest.mark.models


def synthetic_page() -> Image.Image:
    img = Image.new("RGB", (800, 1100), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 780, 1080), outline="black", width=6)
    for x0, y0 in ((80, 80), (430, 500)):
        d.ellipse((x0, y0, x0 + 300, y0 + 200), fill="white", outline="black", width=4)
        d.polygon([(x0 + 140, y0 + 195), (x0 + 120, y0 + 270), (x0 + 175, y0 + 195)], fill="black")
        for i in range(4):
            d.line((x0 + 70, y0 + 60 + 25 * i, x0 + 230, y0 + 60 + 25 * i), fill="black", width=8)
    return img


def test_detects_regions_on_synthetic_page() -> None:
    img = synthetic_page()
    regions = BubbleDetector().detect(img, threshold=0.3)
    assert len(regions) >= 1
    for reg in regions:
        b = reg.bbox
        assert b.x + b.w <= img.width and b.y + b.h <= img.height
        assert reg.kind in ("speech", "caption", "sfx")
        assert 0.3 <= reg.score <= 1.0
