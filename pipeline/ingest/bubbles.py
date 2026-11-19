"""Speech-bubble and free-text region detection, panel assignment and in-panel reading order.

``BubbleDetector`` runs the pinned RT-DETRv2 ``manga-bubble-detector`` (labels ``bubble``,
``text_bubble``, ``text_free``) on CPU in float32. Labels map to Bubble kinds through ``KINDS``;
a label not listed there is skipped and logged, never guessed. Boxes are clamped to the image
and rounded to integer pixels; boxes that collapse to zero width or height are dropped.

``assign`` gives each region to the panel it overlaps most (ties: the smaller, more specific
panel, since merged or nested panel boxes overlap). No overlap means ``None``; the caller sends
those to review (ruling R1). ``order_in_panel`` is Task 4's XY-cut on the region boxes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from PIL import Image

from pipeline.common.schemas import BBox
from pipeline.ingest.panels import Direction, reading_order

log = logging.getLogger(__name__)

MODEL_NAME = "manga-bubble-detector"
Kind = Literal["speech", "sfx", "caption"]
KINDS: Mapping[str, Kind] = {"bubble": "speech", "text_bubble": "speech", "text_free": "caption"}


@dataclass(frozen=True)
class Region:
    bbox: BBox
    kind: Kind
    score: float


def _clamped(box: Sequence[float], width: int, height: int) -> BBox | None:
    x0, y0, x1, y1 = (round(v) for v in box)
    x0, x1 = min(max(x0, 0), width), min(max(x1, 0), width)
    y0, y1 = min(max(y0, 0), height), min(max(y1, 0), height)
    if x1 <= x0 or y1 <= y0:
        return None
    return BBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def regions_from_detections(
    boxes: Sequence[Sequence[float]],
    scores: Sequence[float],
    labels: Sequence[int],
    id2label: Mapping[int, str],
    size: tuple[int, int],
) -> list[Region]:
    """Detector output (x0, y0, x1, y1 boxes in pixels) to Regions. ``size`` is (width, height)."""
    width, height = size
    out: list[Region] = []
    for box, score, label in zip(boxes, scores, labels, strict=True):
        name = id2label.get(int(label))
        kind = KINDS.get(name) if name is not None else None
        if kind is None:
            log.warning("skipped detection with unmapped label %r (id %d)", name, int(label))
            continue
        bbox = _clamped(box, width, height)
        if bbox is None:
            continue
        out.append(Region(bbox=bbox, kind=kind, score=float(score)))
    return out


class BubbleDetector:
    """The pinned detector, loaded lazily once per instance via verify-then-load."""

    def __init__(self) -> None:
        self._processor: Any = None
        self._model: Any = None

    def _load(self) -> tuple[Any, Any]:
        if self._model is None:
            from transformers import AutoImageProcessor, AutoModelForObjectDetection

            from pipeline.providers.local import load

            self._processor = load(AutoImageProcessor, MODEL_NAME, weights=False)
            model = load(AutoModelForObjectDetection, MODEL_NAME)
            model.eval()
            self._model = model
        return self._processor, self._model

    def detect(self, img: Image.Image, threshold: float = 0.5) -> list[Region]:
        import torch

        processor, model = self._load()
        rgb = img.convert("RGB")
        with torch.inference_mode():
            inputs = processor(images=rgb, return_tensors="pt")
            outputs = model(**inputs)
            [res] = processor.post_process_object_detection(
                outputs, threshold=threshold, target_sizes=[(rgb.height, rgb.width)]
            )
        id2label = {int(k): str(v) for k, v in model.config.id2label.items()}
        return regions_from_detections(
            boxes=res["boxes"].tolist(),
            scores=res["scores"].tolist(),
            labels=res["labels"].tolist(),
            id2label=id2label,
            size=(rgb.width, rgb.height),
        )


def _overlap(a: BBox, b: BBox) -> int:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0


def assign(regions: Sequence[Region], panels: list[BBox]) -> list[tuple[Region, int | None]]:
    """Each region with the index of the panel it overlaps most, or ``None`` if it overlaps none.

    Ties go to the smaller panel (the more specific one when panel boxes are nested or merged).
    """
    out: list[tuple[Region, int | None]] = []
    for region in regions:
        best: int | None = None
        best_key = (0, 0)
        for i, panel in enumerate(panels):
            area = _overlap(region.bbox, panel)
            key = (area, -(panel.w * panel.h))
            if area > 0 and (best is None or key > best_key):
                best, best_key = i, key
        out.append((region, best))
    return out


def order_in_panel(regions: Sequence[Region], direction: Direction) -> list[int]:
    """Reading order of the regions of one panel: a permutation of their indices (XY-cut)."""
    return reading_order([r.bbox for r in regions], direction)
