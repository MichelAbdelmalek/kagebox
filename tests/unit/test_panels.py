"""Panel detection, XY-cut reading order and manhwa strip slicing on synthetic pages."""

from __future__ import annotations

import itertools

import cv2
import numpy as np
import pytest

from pipeline.common.schemas import BBox
from pipeline.ingest.panels import (
    IRREGULAR_FILL,
    PanelCandidate,
    Slice,
    detect_panels,
    find_panels,
    reading_order,
    slice_strip,
    slice_strip_cuts,
)

W, H = 1000, 1400


def blank(w: int = W, h: int = H) -> np.ndarray:
    return np.full((h, w), 255, dtype=np.uint8)


def frame(img: np.ndarray, x0: int, y0: int, x1: int, y1: int, ink: int = 0) -> None:
    cv2.rectangle(img, (x0, y0), (x1, y1), ink, thickness=4)
    # some interior art so the panel is not only a border
    cv2.line(img, (x0 + 20, y0 + 20), (x1 - 20, y1 - 20), ink, thickness=3)


def near(box: BBox, x: int, y: int, w: int, h: int, tol: int = 8) -> bool:
    return (
        abs(box.x - x) <= tol
        and abs(box.y - y) <= tol
        and abs(box.w - w) <= tol
        and abs(box.h - h) <= tol
    )


def ordered(img: np.ndarray, direction: str) -> list[BBox]:
    boxes = detect_panels(img)
    perm = reading_order(boxes, direction)  # type: ignore[arg-type]
    assert sorted(perm) == list(range(len(boxes)))
    return [boxes[i] for i in perm]


def grid_2x2() -> np.ndarray:
    img = blank()
    frame(img, 40, 40, 480, 680)  # top-left
    frame(img, 520, 40, 960, 680)  # top-right
    frame(img, 40, 720, 480, 1360)  # bottom-left
    frame(img, 520, 720, 960, 1360)  # bottom-right
    return img


TL, TR = (40, 40), (520, 40)
BL, BR = (40, 720), (520, 720)


def corner(box: BBox) -> tuple[int, int]:
    return (round(box.x / 20) * 20, round(box.y / 20) * 20)


def test_grid_2x2_rtl_order() -> None:
    boxes = ordered(grid_2x2(), "rtl")
    assert [corner(b) for b in boxes] == [TR, TL, BR, BL]
    assert near(boxes[0], 518, 38, 445, 645)


def test_grid_2x2_ltr_order() -> None:
    boxes = ordered(grid_2x2(), "ltr")
    assert [corner(b) for b in boxes] == [TL, TR, BL, BR]


def test_grid_2x2_rgb_input() -> None:
    rgb = cv2.cvtColor(grid_2x2(), cv2.COLOR_GRAY2RGB)
    assert len(detect_panels(rgb)) == 4


def test_tall_left_panel_rtl() -> None:
    img = blank()
    frame(img, 40, 40, 480, 1360)  # tall left
    frame(img, 520, 40, 960, 680)  # right top
    frame(img, 520, 720, 960, 1360)  # right bottom
    boxes = ordered(img, "rtl")
    assert [corner(b) for b in boxes] == [(520, 40), (520, 720), (40, 40)]


def test_full_bleed_page_single_panel() -> None:
    img = np.full((H, W), 128, dtype=np.uint8)
    cv2.circle(img, (500, 700), 200, 30, thickness=-1)  # dark art
    cv2.rectangle(img, (300, 1100), (700, 1300), 230, thickness=-1)  # light art, off the border
    assert detect_panels(img) == [BBox(x=0, y=0, w=W, h=H)]


def test_uniform_and_empty_pages_single_panel() -> None:
    assert detect_panels(np.full((H, W), 128, dtype=np.uint8)) == [BBox(x=0, y=0, w=W, h=H)]
    assert detect_panels(blank()) == [BBox(x=0, y=0, w=W, h=H)]


def test_large_framed_panel_counts_as_full_page() -> None:
    img = blank()
    frame(img, 10, 10, 990, 1390)  # >= 90 % of the page
    assert detect_panels(img) == [BBox(x=0, y=0, w=W, h=H)]


def test_noise_specks_ignored() -> None:
    img = grid_2x2()
    rng = np.random.default_rng(7)
    for _ in range(60):  # specks and short scratches in the gutters
        x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
        cv2.circle(img, (x, y), int(rng.integers(1, 6)), 0, thickness=-1)
    cv2.line(img, (490, 100), (510, 140), 0, thickness=2)
    boxes = detect_panels(img)
    assert len(boxes) == 4  # a speck touching a frame may grow that box by a few px
    for x, y in (TL, TR, BL, BR):
        assert any(abs(b.x - x) <= 15 and abs(b.y - y) <= 15 for b in boxes)


def test_hairline_gap_in_border_is_closed() -> None:
    img = grid_2x2()
    img[300:302, 36:45] = 255  # a 2 px break in the top-left frame's left border
    assert len(detect_panels(img)) == 4


def test_black_paper_page_rtl() -> None:
    # Colour page on black paper: light panels, black gutters (Black Jack p6).
    img = np.zeros((H, W, 3), dtype=np.uint8)
    for x0, y0, x1, y1 in (
        (40, 40, 480, 680),
        (520, 40, 960, 680),
        (40, 720, 480, 1360),
        (520, 720, 960, 1360),
    ):
        cv2.rectangle(img, (x0, y0), (x1, y1), (210, 190, 150), thickness=-1)
        cv2.line(img, (x0 + 30, y0 + 30), (x1 - 30, y1 - 30), (30, 20, 10), thickness=3)
    boxes = ordered(img, "rtl")
    assert [corner(b) for b in boxes] == [TR, TL, BR, BL]


def test_black_paper_panels_bleeding_off_top_and_bottom() -> None:
    # Black Jack p6: black side margins, but the panels run off the top and bottom edges, so
    # only ~63 % of the border ring is black.
    img = np.zeros((H, W, 3), dtype=np.uint8)
    for x0, y0, x1, y1 in ((40, 0, 480, 680), (520, 0, 960, 680), (40, 720, 960, 1399)):
        cv2.rectangle(img, (x0, y0), (x1, y1), (210, 190, 150), thickness=-1)
    boxes = ordered(img, "rtl")
    assert [corner(b) for b in boxes] == [(520, 0), (40, 0), (40, 720)]


def test_dark_full_bleed_page_single_panel() -> None:
    # Dark but not near-black border (night scene in mid tones): not a black-paper page.
    img = np.full((H, W), 70, dtype=np.uint8)
    cv2.circle(img, (500, 700), 200, 200, thickness=-1)
    assert find_panels(img) == [PanelCandidate(BBox(x=0, y=0, w=W, h=H), 1.0, "full_page")]


def test_candidates_reasons() -> None:
    page = BBox(x=0, y=0, w=W, h=H)
    assert find_panels(blank()) == [PanelCandidate(page, 1.0, "no_components")]
    assert find_panels(np.zeros((H, W), dtype=np.uint8)) == [
        PanelCandidate(page, 1.0, "no_components")
    ]
    grey = np.full((H, W), 128, dtype=np.uint8)
    cv2.circle(grey, (500, 700), 200, 30, thickness=-1)
    assert find_panels(grey) == [PanelCandidate(page, 1.0, "full_page")]


def test_rectangle_fill_near_one() -> None:
    cands = find_panels(grid_2x2())
    assert len(cands) == 4
    assert all(c.reason == "component" and c.fill > 0.98 for c in cands)
    assert [c.bbox for c in cands] == detect_panels(grid_2x2())


def test_merged_l_shape_is_irregular() -> None:
    # A character breaking out of the frame joins two panels into one L-shaped component.
    img = blank()
    frame(img, 40, 40, 480, 680)
    frame(img, 520, 40, 960, 680)
    frame(img, 40, 720, 480, 1360)
    cv2.rectangle(img, (470, 600), (530, 780), 0, thickness=-1)  # break-out joins all three
    [merged] = find_panels(img)
    assert merged.reason == "component"
    assert merged.fill < IRREGULAR_FILL


def test_single_component_by_bbox_area_is_whole_page() -> None:
    # Art box covers >= 90 % of the page by bbox but < 90 % by pixels (white narration margin
    # cuts into it from the right, Black Jack p34): the panel is the whole page.
    img = blank()
    cv2.rectangle(img, (0, 0), (949, 1399), 100, thickness=-1)
    cv2.rectangle(img, (500, 300), (999, 1000), 255, thickness=-1)
    cv2.putText(img, "|", (965, 600), cv2.FONT_HERSHEY_SIMPLEX, 1, 0, 2)  # margin text
    assert find_panels(img) == [PanelCandidate(BBox(x=0, y=0, w=W, h=H), 1.0, "full_page")]


def test_painting_split_by_dark_art_on_black_paper_is_one_panel() -> None:
    # Black Jack p5: one painting on black paper. Dark strokes that reach the black margin
    # become "gutter" after inversion and cut it into pieces whose boxes overlap (ruling P4).
    img = np.zeros((H, W, 3), dtype=np.uint8)
    cv2.rectangle(img, (40, 100), (960, 1300), (200, 160, 90), thickness=-1)
    cv2.line(img, (0, 0), (W - 1, H - 1), (0, 0, 0), thickness=24)
    cv2.line(img, (W - 1, 0), (0, H - 1), (0, 0, 0), thickness=24)
    raw = find_panels(img)
    assert len(raw) == 1
    [panel] = raw
    assert panel.reason == "component"
    assert panel.merged  # pieces were unioned: the flow sends it to review (panel_merged)
    assert near(panel.bbox, 40, 100, 921, 1201)
    assert 0.9 < panel.fill <= 1.0  # summed pieces / union box: the strokes are the gap


def test_nested_fragment_merges_into_its_panel() -> None:
    # Black Jack p6: a light shape inside a panel, cut off by dark art, came out as its own
    # candidate inside the panel's box. Nested boxes overlap 100 % of the smaller one.
    img = np.zeros((H, W, 3), dtype=np.uint8)
    cv2.rectangle(img, (520, 40), (960, 1360), (210, 190, 150), thickness=-1)
    cv2.rectangle(img, (40, 40), (480, 1360), (210, 190, 150), thickness=-1)
    cv2.rectangle(img, (600, 300), (900, 1100), (0, 0, 0), thickness=20)  # dark ring
    cv2.line(img, (900, 700), (W - 1, 700), (0, 0, 0), thickness=20)  # ring reaches the margin
    boxes = ordered(img, "rtl")
    assert [corner(b) for b in boxes] == [(520, 40), (40, 40)]
    merged = {corner(c.bbox): c.merged for c in find_panels(img)}
    assert merged == {(520, 40): True, (40, 40): False}


def test_black_paper_page6_layout_keeps_four_panels() -> None:
    # Black Jack p6 layout: wide top panel, tall right panel, two stacked left panels.
    img = np.zeros((H, W, 3), dtype=np.uint8)
    for x0, y0, x1, y1 in (
        (60, 0, 940, 540),
        (540, 580, 940, 1399),
        (60, 580, 500, 960),
        (60, 1000, 500, 1399),
    ):
        cv2.rectangle(img, (x0, y0), (x1, y1), (210, 190, 150), thickness=-1)
        cv2.line(img, (x0 + 30, y0 + 30), (x1 - 30, y1 - 30), (30, 20, 10), thickness=3)
    boxes = ordered(img, "rtl")
    assert [corner(b) for b in boxes] == [(60, 0), (540, 580), (60, 580), (60, 1000)]


def test_white_paper_grid_unchanged_by_merge() -> None:
    cands = find_panels(grid_2x2())
    assert len(cands) == 4 and all(c.reason == "component" for c in cands)
    assert not any(c.merged for c in cands)


def test_touching_and_slightly_overlapping_boxes_stay_separate() -> None:  # ruling P4 (d)
    img = blank()
    # Two frames separated by a thin gutter: boxes touch, never overlap.
    frame(img, 40, 40, 496, 680)
    frame(img, 504, 40, 960, 680)
    # Two panels split by a slanted gutter: their boxes overlap in a 20-row band, about 6 % of
    # the smaller box, under MERGE_OVERLAP.
    upper = np.array([[40, 720], [960, 720], [960, 1020], [40, 1080]])
    lower = np.array([[40, 1120], [960, 1060], [960, 1360], [40, 1360]])
    for poly in (upper, lower):
        cv2.polylines(img, [poly], isClosed=True, color=0, thickness=4)
        x0, y0 = poly.min(axis=0)
        x1, y1 = poly.max(axis=0)
        cv2.line(img, (int(x0) + 30, int(y0) + 30), (int(x1) - 30, int(y1) - 40), 0, thickness=3)
    cands = find_panels(img)
    assert len(cands) == 4 and not any(c.merged for c in cands)


def test_l_shaped_panel_and_notch_panel_merge_and_are_marked() -> None:
    # An L-shaped panel's box always contains a panel in its notch (100 % of the smaller box),
    # so ruling P4 merges them; the flag makes sure a reviewer sees it.
    img = blank()
    pts = np.array([[40, 40], [960, 40], [960, 1360], [600, 1360], [600, 1000], [40, 1000]])
    cv2.polylines(img, [pts], isClosed=True, color=0, thickness=4)
    cv2.line(img, (60, 60), (940, 980), 0, thickness=3)
    frame(img, 40, 1040, 560, 1360)
    [cand] = find_panels(img)
    assert cand.merged and cand.reason == "component"


def test_detect_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        detect_panels(np.zeros((10, 10, 2), dtype=np.uint8))
    with pytest.raises(ValueError):
        detect_panels(np.zeros((10, 10), dtype=np.float32))


# ---- reading_order on bare boxes (Task 5 reuses it for regions inside a panel) ----------------


def b(x: int, y: int, w: int, h: int) -> BBox:
    return BBox(x=x, y=y, w=w, h=h)


def test_reading_order_empty_and_single() -> None:
    assert reading_order([], "rtl") == []
    assert reading_order([b(0, 0, 5, 5)], "ltr") == [0]


def test_reading_order_widest_horizontal_gap_first() -> None:
    # Three rows; the gap under row 1 is narrow, under row 2 wide. Result is still top to bottom.
    boxes = [b(0, 220, 50, 50), b(0, 0, 50, 100), b(0, 105, 50, 50)]
    assert reading_order(boxes, "rtl") == [1, 2, 0]


def test_reading_order_vertical_split_rtl_and_ltr() -> None:
    boxes = [b(0, 0, 40, 40), b(100, 0, 40, 40), b(50, 0, 40, 40)]
    assert reading_order(boxes, "rtl") == [1, 2, 0]
    assert reading_order(boxes, "ltr") == [0, 2, 1]


def test_reading_order_overlap_fallback() -> None:
    # Pairwise overlapping in both axes, so no XY cut exists: fall back to bands of 1/20 of the
    # boxes' height span, then right edge (rtl) or left edge (ltr). Spans 110 px: bands of 5.5 px.
    boxes = [b(0, 0, 60, 60), b(40, 2, 60, 60), b(20, 50, 60, 60)]
    assert reading_order(boxes, "rtl") == [1, 0, 2]
    assert reading_order(boxes, "ltr") == [0, 1, 2]


def test_reading_order_is_permutation_for_random_boxes() -> None:
    rng = np.random.default_rng(3)
    boxes = [
        b(int(rng.integers(0, 900)), int(rng.integers(0, 900)), int(rng.integers(1, 200)), 30)
        for _ in range(50)
    ]
    for d in ("rtl", "ltr"):
        assert sorted(reading_order(boxes, d)) == list(range(50))  # type: ignore[arg-type]


# ---- manhwa strips ----------------------------------------------------------------------------


def test_slice_strip_cuts_at_gutters() -> None:
    img = np.full((7000, 800), 255, dtype=np.uint8)
    # art blocks with gutters: 1800-1900 (100 px), 2600-2640 (40 px), 4500-4900 (400 px)
    for y0, y1 in ((0, 1800), (1900, 2600), (2640, 4500), (4900, 7000)):
        cv2.rectangle(img, (20, y0 + 5), (780, y1 - 5), 0, thickness=-1)
    ranges = slice_strip(img, max_height=3000)
    assert ranges[0][0] == 0 and ranges[-1][1] == 7000
    assert all(a[1] == c[0] for a, c in itertools.pairwise(ranges))
    assert all(0 < y1 - y0 <= 3000 for y0, y1 in ranges)
    # First window [0, 3000]: widest gutter is 1800-1900, so cut in it, not in 2600-2640.
    assert 1800 <= ranges[0][1] <= 1900
    # Next window from ~1850: 4500-4900 is beyond 1850+3000? No: 4850 > 4500, so cut there.
    assert 4500 <= ranges[1][1] <= 4900
    assert len(ranges) == 3


def test_slice_strip_hard_cut_without_gutter() -> None:
    img = np.zeros((7000, 100), dtype=np.uint8)
    img[::2, ::2] = 255  # texture, never a white row
    assert slice_strip(img, max_height=3000) == [(0, 3000), (3000, 6000), (6000, 7000)]


def test_slice_strip_short_image_single_range() -> None:
    assert slice_strip(blank(), max_height=3000) == [(0, H)]


def test_slice_strip_rejects_bad_max_height() -> None:
    with pytest.raises(ValueError):
        slice_strip(blank(), max_height=0)


def test_slice_strip_cuts_report_hard_cuts() -> None:
    img = np.zeros((7000, 100), dtype=np.uint8)
    img[::2, ::2] = 255  # texture, never a white row: both cuts are hard
    assert slice_strip_cuts(img, max_height=3000) == [
        Slice(0, 3000, hard_cut_after=True),
        Slice(3000, 6000, hard_cut_after=True),
        Slice(6000, 7000, hard_cut_after=False),
    ]


def test_slice_strip_cuts_at_gutters_are_not_hard() -> None:
    img = np.full((7000, 800), 255, dtype=np.uint8)
    for y0, y1 in ((0, 1800), (1900, 2600), (2640, 4500), (4900, 7000)):
        cv2.rectangle(img, (20, y0 + 5), (780, y1 - 5), 0, thickness=-1)
    cuts = slice_strip_cuts(img, max_height=3000)
    assert [(c.y0, c.y1) for c in cuts] == slice_strip(img, max_height=3000)
    assert not any(c.hard_cut_after for c in cuts)


def test_slice_strip_cuts_mixed() -> None:
    img = np.zeros((6000, 100), dtype=np.uint8)  # all ink ...
    img[2000:2100] = 255  # ... with one gutter in the first window only
    cuts = slice_strip_cuts(img, max_height=3000)
    assert [c.hard_cut_after for c in cuts] == [False, True, False]
    assert 2000 <= cuts[0].y1 <= 2100 and cuts[1].y1 == cuts[0].y1 + 3000
