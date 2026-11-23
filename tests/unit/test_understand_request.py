"""The request builder: masked image only, fail-closed mask, cache key inputs, margin."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image

import pipeline.understand.request as request_mod
from pipeline.common.schemas import BBox, Bubble, Provenance
from pipeline.ingest.bubbles import Region
from pipeline.providers.base import ModelSpec
from pipeline.understand.mask import MAX_MARGIN_PX, mask_panel, text_mask_boxes
from pipeline.understand.request import (
    MaskFailed,
    input_hashes,
    panel_ocr_text,
    params_hash,
    prepare_panel,
    scene_params,
)
from pipeline.understand.settings import UnderstandSettings, load_settings

PANEL = BBox(x=100, y=200, w=60, h=40)
BG = (200, 210, 220)
SENTINEL = "SENTINEL-OCR-55aa"
SPEC = ModelSpec("understand", "local", "toy-vlm", "r" * 40, "Apache-2.0", "https://x")


def crop_png(size: tuple[int, int] = (60, 40)) -> bytes:
    im = Image.new("RGB", size, BG)
    for x in range(size[0]):
        for y in range(10, 30):
            im.putpixel((x, y), (0, 0, 0) if (x + y) % 2 else (255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def px(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


class Det:
    def __init__(self, regions: list[Region] | None = None, exc: Exception | None = None) -> None:
        self.regions, self.exc, self.calls = regions or [], exc, 0

    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.regions


def region(x: int, y: int, w: int, h: int) -> Region:
    return Region(BBox(x=x, y=y, w=w, h=h), "speech", 0.9, "bubble", None)


def bubble(order: int, text: str) -> Bubble:
    prov = Provenance(
        stage="ingest",
        tool="t",
        tool_version="1",
        license="MIT",
        provider="none",
        params_hash="a" * 64,
        created_at=datetime.now(UTC),
    )
    return Bubble(
        id=f"bubble_{order}",
        panel_id="panel_1",
        bbox=BBox(x=1, y=1, w=5, h=5),
        kind="speech",
        text=text,
        lang="ja",
        ocr_confidence=0.9,
        ocr_engine="manga-ocr",
        speaker={"value": None, "panel_ids": []},  # type: ignore[arg-type]
        reading_order=order,
        provenance=prov,
    )


# ---- text and hashes ---------------------------------------------------------------------


def test_panel_ocr_text_is_in_reading_order_newline_joined() -> None:
    assert panel_ocr_text([bubble(2, "second"), bubble(1, "first")]) == "first\nsecond"
    assert panel_ocr_text([]) == ""


def test_input_hashes_are_the_masked_image_and_the_text() -> None:
    assert input_hashes(b"png", "hi") == [
        hashlib.sha256(b"png").hexdigest(),
        hashlib.sha256(b"hi").hexdigest(),
    ]


def settings(**kw: Any) -> UnderstandSettings:
    return load_settings().model_copy(update=kw)


@pytest.mark.parametrize(
    ("change", "kind"),
    [
        ({"dtype": "float32"}, "settings"),
        ({"max_pixels": 200_000}, "settings"),
        ({"max_new_tokens": 256}, "settings"),
        ({"mask_detect_threshold": 0.5}, "settings"),
        ({"margin_px": 8}, "settings"),
        ({"model": "other"}, "spec"),
        ({"revision": "s" * 40}, "spec"),
        ({"prompt": "b" * 64}, "prompt"),
    ],
)
def test_every_input_changes_the_params_hash(change: dict[str, Any], kind: str) -> None:
    base = params_hash(scene_params(settings(), SPEC, "a" * 64))
    s, spec, prompt = settings(), SPEC, "a" * 64
    if kind == "settings":
        s = settings(**change)
    elif kind == "spec":
        spec = ModelSpec(**{**SPEC.__dict__, **change})
    else:
        prompt = change["prompt"]
    assert params_hash(scene_params(s, spec, prompt)) != base


def test_schema_and_attention_are_part_of_the_params_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    base = scene_params(settings(), SPEC, "a" * 64)
    assert base["attn"] == "eager" and len(base["schema_sha256"]) == 64
    monkeypatch.setattr(request_mod, "schema_sha256", lambda: "c" * 64)
    assert params_hash(scene_params(settings(), SPEC, "a" * 64)) != params_hash(base)
    monkeypatch.setattr(request_mod, "ATTN_IMPLEMENTATION", "sdpa")
    assert scene_params(settings(), SPEC, "a" * 64)["attn"] == "sdpa"


# ---- fail-closed mask --------------------------------------------------------------------


def test_prepare_panel_sends_the_masked_image_and_hashes_it() -> None:
    det = Det([region(10, 10, 20, 20)])
    prep = prepare_panel(crop_png(), PANEL, [bubble(1, SENTINEL)], [], det, settings(margin_px=0))
    out = px(prep.png)
    assert out.getpixel((10, 10)) == BG and out.getpixel((29, 29)) == BG
    assert prep.png != crop_png()
    assert prep.ocr_text == SENTINEL
    assert prep.input_hashes == input_hashes(prep.png, SENTINEL)


def test_unmasked_image_is_never_returned_when_the_mask_fails() -> None:
    boom = Det(exc=RuntimeError(f"detector exploded {SENTINEL}"))
    with pytest.raises(MaskFailed) as err:
        prepare_panel(crop_png(), PANEL, [], [], boom, settings())
    assert err.value.cause == "RuntimeError"
    assert SENTINEL not in str(err.value) and SENTINEL not in repr(err.value)


def test_missing_panel_box_raises_instead_of_falling_back() -> None:
    det = Det()
    with pytest.raises(MaskFailed):
        prepare_panel(crop_png(), None, [], [], det, settings())
    assert det.calls == 0


def test_non_png_crop_fails_closed() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40)).save(buf, format="JPEG")
    with pytest.raises(MaskFailed):
        prepare_panel(buf.getvalue(), PANEL, [], [], Det(), settings())


# ---- margin ------------------------------------------------------------------------------


def test_margin_grows_every_box_and_is_clamped_to_the_crop() -> None:
    det = Det([region(10, 10, 20, 20)])
    boxes = text_mask_boxes(crop_png(), PANEL, [BBox(x=150, y=210, w=40, h=20)], det, 0.35, 4)
    assert BBox(x=6, y=6, w=28, h=28) in boxes  # detected, grown by 4 on every side
    assert BBox(x=46, y=6, w=14, h=28) in boxes  # stored box: grown, clamped at the right edge
    out = px(mask_panel(crop_png(), PANEL, [], det, 0.35, 4))
    assert out.getpixel((6, 6)) == BG and out.getpixel((33, 33)) == BG


def test_margin_zero_keeps_the_exact_box() -> None:
    boxes = text_mask_boxes(crop_png(), PANEL, [], Det([region(10, 10, 20, 20)]), 0.35, 0)
    assert boxes == [BBox(x=10, y=10, w=20, h=20)]


@pytest.mark.parametrize("bad", [-1, MAX_MARGIN_PX + 1])
def test_margin_is_validated(bad: int) -> None:
    with pytest.raises(ValueError, match="margin_px"):
        text_mask_boxes(crop_png(), PANEL, [], Det(), 0.35, bad)


def test_margin_setting_is_bounded_and_defaults_to_four() -> None:
    assert load_settings().margin_px == 4
    for bad in (-1, 33):
        with pytest.raises(ValueError, match="margin_px"):
            UnderstandSettings(**{**load_settings().model_dump(), "margin_px": bad})
