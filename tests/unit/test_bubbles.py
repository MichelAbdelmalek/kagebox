"""Bubble/text-region mapping, panel assignment and in-panel order, without the model."""

from __future__ import annotations

import logging

import pytest

from pipeline.common.schemas import BBox
from pipeline.ingest.bubbles import Region, assign, order_in_panel, regions_from_detections

ID2LABEL = {0: "bubble", 1: "text_bubble", 2: "text_free"}


def r(x: int, y: int, w: int, h: int, kind: str = "speech") -> Region:
    return Region(bbox=BBox(x=x, y=y, w=w, h=h), kind=kind, score=0.9)  # type: ignore[arg-type]


class FakeDetector:
    """Stands in for BubbleDetector: returns fixed regions."""

    def __init__(self, regions: list[Region]) -> None:
        self.regions = regions

    def detect(self, img: object, threshold: float = 0.5) -> list[Region]:
        return [x for x in self.regions if x.score >= threshold]


LEFT = BBox(x=0, y=0, w=500, h=500)
RIGHT = BBox(x=500, y=0, w=500, h=500)


def test_overlapping_two_panels_goes_to_larger_overlap() -> None:
    region = r(420, 100, 200, 100)  # 80 in LEFT, 120 in RIGHT
    [(got, idx)] = assign(FakeDetector([region]).detect(None), [LEFT, RIGHT])
    assert got == region
    assert idx == 1


def test_overlap_tie_goes_to_smaller_panel() -> None:
    big = BBox(x=0, y=0, w=1000, h=1000)
    small = BBox(x=100, y=100, w=300, h=300)  # nested, e.g. a merged box around it
    [(_, idx)] = assign([r(150, 150, 50, 50)], [big, small])
    assert idx == 1


def test_free_text_assigned_to_containing_panel() -> None:
    caption = r(600, 20, 40, 300, kind="caption")  # vertical narration inside RIGHT
    [(got, idx)] = assign([caption], [LEFT, RIGHT])
    assert got.kind == "caption"
    assert idx == 1


def test_region_outside_panels_unassigned() -> None:
    panels = [BBox(x=0, y=0, w=400, h=400), BBox(x=0, y=600, w=400, h=400)]
    [(_, idx)] = assign([r(10, 450, 100, 100)], panels)  # in the gutter
    assert idx is None
    assert assign([r(10, 10, 5, 5)], []) == [(r(10, 10, 5, 5), None)]


def test_rtl_bubble_order() -> None:
    # top row right-to-left in manga, then the row below
    regions = [r(50, 50, 100, 100), r(300, 50, 100, 100), r(300, 250, 100, 100)]
    assert order_in_panel(regions, "rtl") == [1, 0, 2]
    assert order_in_panel(regions, "ltr") == [0, 1, 2]
    assert order_in_panel([], "rtl") == []


def test_order_uses_task4_xy_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.ingest.bubbles as bubbles

    seen: list[object] = []

    def fake(boxes: object, direction: object) -> list[int]:
        seen.append((boxes, direction))
        return [0]

    monkeypatch.setattr(bubbles, "reading_order", fake)
    assert order_in_panel([r(0, 0, 10, 10)], "ltr") == [0]
    assert seen == [([BBox(x=0, y=0, w=10, h=10)], "ltr")]


def test_labels_map_to_kinds() -> None:
    got = regions_from_detections(
        boxes=[(10.0, 10.0, 50.0, 50.0), (60.0, 10.0, 90.0, 50.0), (10.0, 60.0, 20.0, 95.0)],
        scores=[0.9, 0.8, 0.7],
        labels=[0, 1, 2],
        id2label=ID2LABEL,
        size=(100, 100),
    )
    assert [g.kind for g in got] == ["speech", "speech", "caption"]
    assert got[0].bbox == BBox(x=10, y=10, w=40, h=40)
    assert got[1].score == pytest.approx(0.8)


def test_unknown_label_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pipeline.ingest.bubbles"):
        got = regions_from_detections(
            boxes=[(10.0, 10.0, 50.0, 50.0)],
            scores=[0.9],
            labels=[7],
            id2label={**ID2LABEL, 7: "onomatopoeia"},
            size=(100, 100),
        )
    assert got == []
    assert "onomatopoeia" in caplog.text


def test_boxes_clamped_and_degenerate_dropped() -> None:
    got = regions_from_detections(
        boxes=[(-5.0, -3.0, 120.0, 40.0), (50.0, 50.0, 50.4, 80.0), (99.6, 10.0, 130.0, 20.0)],
        scores=[0.9, 0.9, 0.9],
        labels=[0, 0, 0],
        id2label=ID2LABEL,
        size=(100, 100),
    )
    assert [g.bbox for g in got] == [BBox(x=0, y=0, w=100, h=40)]
