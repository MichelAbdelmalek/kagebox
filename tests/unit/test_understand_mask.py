from __future__ import annotations

import io
from collections.abc import Sequence

import pytest
from PIL import Image

from pipeline.common.schemas import BBox
from pipeline.common.uploads import UploadRejected
from pipeline.understand.mask import mask_text_regions

PANEL = BBox(x=100, y=200, w=60, h=40)  # the crop is 60x40, located at (100, 200) on the page
BG = (200, 210, 220)
INK = (0, 0, 0)


def crop_png(size: tuple[int, int] = (60, 40)) -> bytes:
    im = Image.new("RGB", size, BG)
    for x in range(size[0]):  # noise in the middle so a fill is distinguishable
        for y in range(10, 30):
            im.putpixel((x, y), INK if (x + y) % 2 else (255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def pixels(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def box(x: int, y: int, w: int, h: int) -> BBox:
    return BBox.model_construct(x=x, y=y, w=w, h=h)  # allows zero or negative sizes


def run(boxes: Sequence[BBox], png: bytes | None = None) -> Image.Image:
    return pixels(mask_text_regions(png or crop_png(), boxes, PANEL))


def test_box_inside_is_filled_flat_with_border_median_and_rest_untouched() -> None:
    src = pixels(crop_png())
    out = run([box(110, 210, 20, 20)])  # crop coords (10, 10, 20, 20)
    for x in range(10, 30):
        for y in range(10, 30):
            assert out.getpixel((x, y)) == BG
    for x, y in [(9, 10), (30, 10), (10, 9), (10, 30), (0, 0), (59, 39)]:
        assert out.getpixel((x, y)) == src.getpixel((x, y))


def test_box_partly_outside_is_clamped() -> None:
    out = run([box(90, 190, 30, 30)])  # crop coords (-10, -10) .. (20, 20)
    assert out.getpixel((0, 0)) == BG and out.getpixel((19, 19)) == BG
    assert out.getpixel((20, 20)) == pixels(crop_png()).getpixel((20, 20))


def test_box_outside_is_ignored() -> None:
    png = crop_png()
    out = mask_text_regions(png, [box(0, 0, 50, 50), box(300, 300, 10, 10)], PANEL)
    assert pixels(out).tobytes() == pixels(png).tobytes()


def test_empty_list_returns_identical_pixels() -> None:
    png = crop_png()
    assert pixels(mask_text_regions(png, [], PANEL)).tobytes() == pixels(png).tobytes()


def test_huge_box_covers_everything() -> None:
    out = run([box(0, 0, 10**9, 10**9)])
    assert out.getcolors() == [(60 * 40, BG)]


def test_zero_or_negative_size_ignored() -> None:
    png = crop_png()
    out = run([box(110, 210, 0, 20), box(110, 210, 20, 0), box(110, 210, -5, 5)], png)
    assert out.tobytes() == pixels(png).tobytes()


def test_fill_colour_is_the_border_median() -> None:
    im = Image.new("RGB", (60, 40), (10, 20, 30))
    for x in range(60):  # top row differs: the median over the whole border stays the majority
        im.putpixel((x, 0), (250, 250, 250))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = pixels(mask_text_regions(buf.getvalue(), [box(110, 210, 5, 5)], PANEL))
    assert out.getpixel((12, 12)) == (10, 20, 30)


def test_crop_scaled_relative_to_panel_bbox() -> None:
    # a crop twice the size of its page box: boxes scale with it
    out = pixels(mask_text_regions(crop_png((120, 80)), [box(110, 210, 10, 10)], PANEL))
    assert out.getpixel((20, 20)) == BG and out.getpixel((39, 39)) == BG
    assert out.getpixel((60, 60)) == pixels(crop_png((120, 80))).getpixel((60, 60))


def test_output_is_png_and_same_size() -> None:
    data = mask_text_regions(crop_png(), [box(110, 210, 5, 5)], PANEL)
    im = Image.open(io.BytesIO(data))
    assert im.format == "PNG" and im.size == (60, 40)


def test_non_png_and_garbage_refused() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="JPEG")
    for bad in (buf.getvalue(), b"not an image", b""):
        with pytest.raises(UploadRejected):
            mask_text_regions(bad, [], PANEL)


def test_huge_canvas_refused_before_decode() -> None:
    buf = io.BytesIO()
    Image.new("1", (20_000, 20_000)).save(buf, format="PNG")
    with pytest.raises(UploadRejected):
        mask_text_regions(buf.getvalue(), [], PANEL)
