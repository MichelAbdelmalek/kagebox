"""Real pinned OCR models: PP-OCRv5 (en) on Pillow-drawn text, manga-ocr on a sample bubble.

The Japanese test uses page 7 of the public-domain Black Jack sample when it is present under
data/ (gitignored, never downloaded by a test); otherwise it is skipped with the reason.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from pipeline.ingest.ocr import MangaOcr, PpOcr

pytestmark = pytest.mark.models

SAMPLE_REL = "data/fixtures/blackjack-v1/work/001bj.pdf"
SAMPLE = Path(__file__).resolve().parents[2] / SAMPLE_REL
SAMPLE_PAGE = 7


def drawn_text(text: str, scale: int = 6) -> Image.Image:
    """`text` in Pillow's built-in bitmap font (no font file), upscaled so it is legible."""
    font = ImageFont.load_default()
    x0, y0, x1, y1 = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=font)
    pad = 6
    small = Image.new("RGB", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), "white")
    ImageDraw.Draw(small).text((pad - x0, pad - y0), text, fill="black", font=font)
    big = small.resize((small.width * scale, small.height * scale), Image.Resampling.NEAREST)
    canvas = Image.new("RGB", (big.width + 80, big.height + 80), "white")  # bubble-like margin
    canvas.paste(big, (40, 40))
    return canvas


def squash(s: str) -> str:
    return "".join(s.split())


def test_en_reads_hello() -> None:
    text, conf = PpOcr("en").read(drawn_text("HELLO"))
    assert squash(text) == "HELLO", (text, conf)
    assert 0.0 < conf <= 1.0


def is_japanese(ch: str) -> bool:
    name = unicodedata.name(ch, "")
    return name.startswith(("HIRAGANA", "KATAKANA", "CJK UNIFIED", "CJK COMPATIBILITY"))


def test_ja_reads_a_bubble_of_the_sample_page() -> None:
    if not SAMPLE.is_file():
        pytest.skip(f"sample manga not present: {SAMPLE_REL}")
    from pipeline.ingest.bubbles import BubbleDetector
    from pipeline.ingest.sources import iter_pages

    [(_, page)] = list(iter_pages(SAMPLE, "pdf", SAMPLE_PAGE, SAMPLE_PAGE))
    speech = [r for r in BubbleDetector().detect(page) if r.kind == "speech"]
    assert speech, "no speech region detected on the sample page"
    box = speech[0].text_bbox or speech[0].bbox
    crop = page.crop((box.x, box.y, box.x + box.w, box.y + box.h))

    text, conf = MangaOcr().read(crop)
    assert text.strip(), (box, conf)
    assert any(is_japanese(c) for c in text), text
    assert 0.0 < conf <= 1.0
