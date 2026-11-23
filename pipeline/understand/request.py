"""Build the request for one panel: the masked image, the cache key and the input hashes.

Injection defence, fail closed: the image sent to the model is ALWAYS the output of
``mask_panel`` (every text region flat-filled), and no OCR text is sent. Any mask or detector
error, or a missing panel box, raises ``MaskFailed`` and the caller records the panel as failed;
the model is never called with an unmasked image.

The cache key is built from what the model actually sees: ``params_hash`` covers the prompt, the
reading schema, the model, the load options and the mask settings; ``input_hashes`` start with the
hash of the MASKED image bytes, so a re-detection that changes the mask changes the key. The OCR
text is hashed too (it stays in the database and in ``input_hashes``, never in a request).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pipeline.common.schemas import BBox, Bubble
from pipeline.providers.base import ModelSpec
from pipeline.understand.contract import schema_sha256
from pipeline.understand.mask import Detector, mask_panel
from pipeline.understand.settings import ATTN_IMPLEMENTATION, UnderstandSettings

__all__ = [
    "MaskFailed",
    "PreparedPanel",
    "input_hashes",
    "panel_ocr_text",
    "params_hash",
    "prepare_panel",
    "scene_params",
]


class MaskFailed(RuntimeError):
    """The text mask could not be built. ``cause`` is an exception class name, nothing else."""

    def __init__(self, cause: str) -> None:
        super().__init__("the text mask could not be built")
        self.cause = cause


@dataclass(frozen=True)
class PreparedPanel:
    png: bytes  # the masked image: the only image the model ever gets
    ocr_text: str  # untrusted; stored and hashed, never sent
    input_hashes: list[str]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def panel_ocr_text(bubbles: Sequence[Bubble]) -> str:
    """The bubble texts in reading order, newline-joined."""
    return "\n".join(b.text for b in sorted(bubbles, key=lambda b: b.reading_order))


def scene_params(settings: UnderstandSettings, spec: ModelSpec, prompt_sha: str) -> dict[str, Any]:
    """Every input of the model call except the panel itself."""
    return {
        "prompt_sha256": prompt_sha,
        "schema_sha256": schema_sha256(),
        "model": spec.model,
        "revision": spec.revision,
        "dtype": settings.dtype,
        "attn": ATTN_IMPLEMENTATION,
        "max_pixels": settings.max_pixels,
        "max_new_tokens": settings.max_new_tokens,
        "mask_detect_threshold": settings.mask_detect_threshold,
        "margin_px": settings.margin_px,
    }


def params_hash(params: dict[str, Any]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _sha(canonical.encode("utf-8"))


def input_hashes(sent_png: bytes, ocr_text: str) -> list[str]:
    """[hash of the masked image sent, hash of the panel's OCR text]."""
    return [_sha(sent_png), _sha(ocr_text.encode("utf-8"))]


def prepare_panel(
    crop_png: bytes,
    panel_bbox: BBox | None,
    bubbles: Sequence[Bubble],
    page_boxes: Sequence[BBox],
    detector: Detector,
    settings: UnderstandSettings,
) -> PreparedPanel:
    """The masked image, the OCR text and the input hashes of one panel, or ``MaskFailed``."""
    if panel_bbox is None:
        raise MaskFailed("MissingPanelBox")
    try:
        png = mask_panel(
            crop_png,
            panel_bbox,
            page_boxes,
            detector,
            settings.mask_detect_threshold,
            settings.margin_px,
        )
    except Exception as exc:  # detector, decoder, anything: fail closed, keep only the class
        raise MaskFailed(type(exc).__name__) from None
    text = panel_ocr_text(bubbles)
    return PreparedPanel(png, text, input_hashes(png, text))
