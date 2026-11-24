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


# ---- text_mask_boxes: stored boxes of the whole page UNION detection on the crop --------

from PIL import Image as _Image  # noqa: E402

from pipeline.ingest.bubbles import Region  # noqa: E402
from pipeline.understand.mask import mask_panel, text_mask_boxes  # noqa: E402


class FakeDetector:
    def __init__(self, regions: list[Region] | None = None) -> None:
        self.regions = regions or []
        self.calls: list[tuple[tuple[int, int], float]] = []

    def detect(self, img: _Image.Image, threshold: float = 0.5) -> list[Region]:
        self.calls.append((img.size, threshold))
        return self.regions


def region(bbox: BBox, text: BBox | None = None, kind: str = "speech") -> Region:
    return Region(bbox, kind, 0.9, "bubble", text)  # type: ignore[arg-type]


def test_straddling_bubble_stored_on_the_neighbour_is_masked_here() -> None:
    # stored page box spans x 150..190; this panel covers x 100..160: only 10 px of it is inside
    boxes = text_mask_boxes(crop_png(), PANEL, [box(150, 210, 40, 20)], FakeDetector(), margin_px=0)
    assert boxes == [BBox(x=50, y=10, w=10, h=20)]  # crop coordinates, clamped to the crop
    out = pixels(mask_panel(crop_png(), PANEL, [box(150, 210, 40, 20)], FakeDetector()))
    assert out.getpixel((55, 15)) == BG


def test_stored_boxes_outside_the_panel_are_ignored() -> None:
    assert (
        text_mask_boxes(crop_png(), PANEL, [box(0, 0, 50, 50), box(300, 300, 9, 9)], FakeDetector())
        == []
    )


def test_unstored_region_is_still_masked_by_detection() -> None:
    det = FakeDetector([region(BBox(x=10, y=10, w=20, h=20), BBox(x=12, y=12, w=10, h=10))])
    out = pixels(mask_panel(crop_png(), PANEL, [], det, threshold=0.35))
    assert out.getpixel((10, 10)) == BG and out.getpixel((29, 29)) == BG  # the outer balloon box
    assert det.calls == [((60, 40), 0.35)]  # the crop itself is detected, at the given threshold


def test_detected_text_box_and_every_kind_are_masked() -> None:
    det = FakeDetector(
        [
            region(BBox(x=0, y=0, w=5, h=5), None, "speech"),
            region(BBox(x=20, y=30, w=8, h=8), BBox(x=40, y=30, w=8, h=8), "caption"),
        ]
    )
    boxes = text_mask_boxes(crop_png(), PANEL, [], det, margin_px=0)
    assert BBox(x=40, y=30, w=8, h=8) in boxes and BBox(x=20, y=30, w=8, h=8) in boxes
    assert BBox(x=0, y=0, w=5, h=5) in boxes


def test_nothing_detected_nothing_stored_image_unchanged() -> None:
    png = crop_png()
    assert pixels(mask_panel(png, PANEL, [], FakeDetector())).tobytes() == pixels(png).tobytes()


def test_output_png_has_no_metadata() -> None:
    buf = io.BytesIO()
    _Image.open(io.BytesIO(crop_png())).save(buf, format="PNG", icc_profile=b"x" * 64)
    assert "icc_profile" in _Image.open(io.BytesIO(buf.getvalue())).info
    out = _Image.open(io.BytesIO(mask_text_regions(buf.getvalue(), [], PANEL)))
    assert "icc_profile" not in out.info
