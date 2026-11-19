"""Classical panel detection, XY-cut reading order and manhwa strip slicing.

``find_panels`` finds panel candidates on one page: Otsu ink mask, 3x3 dilation to close hairline
breaks, paper flooded in from the page border is the gutter, and every remaining connected
component with at least ``MIN_PANEL_FRACTION`` of the page's pixels is a panel. No component, or
one whose bounding box covers at least ``FULL_PAGE_FRACTION`` of the page, means a single panel
that is the whole page (full-bleed art; margin narration stays inside it). Each candidate says
why it exists and how much of its box it fills, so callers can send doubtful ones to review
(fill below ``IRREGULAR_FILL`` usually means panels merged by art breaking out of a frame).
``detect_panels`` is the same, boxes only.

Overlapping candidates are merged (ruling P4): panels do not overlap, so two candidates whose
boxes overlap by more than ``MERGE_OVERLAP`` of the smaller box are pieces of one panel (a
painting cut by dark strokes that reach a black margin, or a fragment nested in its panel).
Union-find merges chains; the merged box is the union, its fill the summed component pixels
over the union box, and the whole-page rule is applied again to the merged result. A merged
candidate is marked ``merged`` (with its piece count): pieces of different panels can fill
their union well, so the fill test alone would let such a merge through silently; callers
send every merged candidate to review. Merging
repeats until no two boxes overlap that much, since a union can reach a box its parts missed.

Decisions beyond the plain algorithm, both made on real pages:

- ``PAPER_MIN``: on light paper, a pixel darker than 160 is never gutter, whatever Otsu says.
  Without it Otsu on a full-bleed mid-grey page calls the grey "white" and the dark art a panel.
  Risk: scans of very dark or yellowed paper (gutters below 160) lose their gutters, and the
  page degrades to one full-page panel (reason ``full_page``, sent to review).
- Black paper: when at least ``DARK_BORDER_SHARE`` (half) of a thin border ring is near-black
  (<= ``DARK_BORDER_LEVEL``), the page is inverted first so the black gutters are the "paper".
  Half, not most: panels that bleed off two edges leave only ~60-70 % of the ring black.
  A mid-tone dark border (night scene) is not near-black and keeps the light-paper path.

``reading_order`` is pure (boxes only, no image) so it also orders regions inside a panel.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

import cv2
import numpy as np
import numpy.typing as npt

from pipeline.common.schemas import BBox

Direction = Literal["rtl", "ltr"]
Reason = Literal["component", "full_page", "no_components"]

MIN_PANEL_FRACTION = 0.02
FULL_PAGE_FRACTION = 0.90
FALLBACK_BANDS = 20  # overlap fallback: rows are 1/20 of the boxes' height span
IRREGULAR_FILL = 0.85  # component pixels / bbox area below this: likely merged panels
PAPER_MIN = 160  # see module docstring
BORDER_RING_FRACTION = 0.01  # ring width: 1 % of the short side, at least 2 px
DARK_BORDER_LEVEL = 50
DARK_BORDER_SHARE = 0.50  # i.e. the ring's median is near-black
MERGE_OVERLAP = 0.10  # share of the smaller box: above it, two candidates are one panel

U8 = npt.NDArray[np.uint8]


def _gray(img: npt.NDArray[np.generic]) -> U8:
    if img.dtype != np.uint8:
        raise ValueError("expected a uint8 image")
    u8: U8 = img.astype(np.uint8, copy=False)
    if u8.ndim == 2:
        return u8
    if u8.ndim == 3 and u8.shape[2] == 3:
        return cv2.cvtColor(u8, cv2.COLOR_RGB2GRAY).astype(np.uint8, copy=False)
    raise ValueError("expected a grayscale (H, W) or RGB (H, W, 3) image")


def _ink(gray: U8) -> U8:
    """1 where ink, else 0: darker than both the Otsu threshold and ``PAPER_MIN``."""
    otsu = 0.0
    if int(gray.min()) != int(gray.max()):  # Otsu is undefined on a uniform image
        otsu, _ = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    thresh = max(otsu, PAPER_MIN - 1)
    return (gray <= thresh).astype(np.uint8)


@dataclass(frozen=True)
class PanelCandidate:
    bbox: BBox
    fill: float  # component pixels / bbox area; 1.0 for a whole-page panel
    reason: Reason
    merged: bool = False  # True when >= 2 overlapping components were unioned (ruling P4)
    pieces: int = 1  # how many components make up this candidate


def _black_paper(gray: U8) -> bool:
    h, w = gray.shape
    r = max(2, int(min(h, w) * BORDER_RING_FRACTION))
    ring = np.concatenate(
        [gray[:r].ravel(), gray[-r:].ravel(), gray[r:-r, :r].ravel(), gray[r:-r, -r:].ravel()]
    )
    return bool(np.mean(ring <= DARK_BORDER_LEVEL) >= DARK_BORDER_SHARE)


def find_panels(img: npt.NDArray[np.generic]) -> list[PanelCandidate]:
    gray = _gray(img)
    if _black_paper(gray):
        gray = (255 - gray).astype(np.uint8)
    h, w = gray.shape
    page = BBox(x=0, y=0, w=w, h=h)
    ink = cv2.dilate(_ink(gray), np.ones((3, 3), np.uint8))

    # Gutter = white (non-ink) pixels 4-connected to the page border.
    n, labels = cv2.connectedComponents((ink == 0).astype(np.uint8), connectivity=4)
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    gutter_labels = np.unique(border[border > 0])
    gutter = np.isin(labels, gutter_labels) & (ink == 0)

    content = (~gutter).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(content, connectivity=8)
    area = float(h * w)
    parts: list[tuple[BBox, int]] = []
    for i in range(1, n):
        x, y, bw, bh, px = (int(v) for v in stats[i])
        if px >= MIN_PANEL_FRACTION * area:
            parts.append((BBox(x=x, y=y, w=bw, h=bh), px))
    cands: list[PanelCandidate] = []
    for box, px, pieces in merge_overlapping(parts):
        if box.w * box.h >= FULL_PAGE_FRACTION * area:
            return [PanelCandidate(page, 1.0, "full_page")]
        fill = px / (box.w * box.h)
        cands.append(PanelCandidate(box, fill, "component", pieces > 1, pieces))
    return cands or [PanelCandidate(page, 1.0, "no_components")]


def _overlap_area(a: BBox, b: BBox) -> int:
    ow = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    oh = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    return ow * oh if ow > 0 and oh > 0 else 0


def _union(a: BBox, b: BBox) -> BBox:
    x0, y0 = min(a.x, b.x), min(a.y, b.y)
    x1, y1 = max(a.x + a.w, b.x + b.w), max(a.y + a.h, b.y + b.h)
    return BBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def _root(parent: list[int], i: int) -> int:
    """Union-find root with path halving."""
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def merge_overlapping(parts: Sequence[tuple[BBox, int]]) -> list[tuple[BBox, int, int]]:
    """Merge (box, pixel count) pairs whose boxes overlap > ``MERGE_OVERLAP`` of the smaller.

    Union-find over all pairs (chains merge), repeated until stable. Returns (box, pixels,
    pieces) in the order of each group's first member; pixel counts add up (components never
    share pixels) and ``pieces`` counts the components in the group.
    """
    items = [(box, px, 1) for box, px in parts]
    while True:
        parent = list(range(len(items)))
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i][0], items[j][0]
                smaller = min(a.w * a.h, b.w * b.h)
                if _overlap_area(a, b) > MERGE_OVERLAP * smaller:
                    ri, rj = _root(parent, i), _root(parent, j)
                    if ri != rj:
                        parent[max(ri, rj)] = min(ri, rj)
        groups: dict[int, tuple[BBox, int, int]] = {}
        for i, (box, px, n) in enumerate(items):
            r = _root(parent, i)
            if r in groups:
                gbox, gpx, gn = groups[r]
                groups[r] = (_union(gbox, box), gpx + px, gn + n)
            else:
                groups[r] = (box, px, n)
        merged = [groups[r] for r in sorted(groups)]
        if len(merged) == len(items):
            return merged
        items = merged


def detect_panels(img: npt.NDArray[np.generic]) -> list[BBox]:
    return [c.bbox for c in find_panels(img)]


# ---------------------------------------------------------------------------------------------
# reading order


def _best_cut(
    idx: list[int], start: Callable[[int], int], end: Callable[[int], int]
) -> tuple[list[int], list[int]] | None:
    """Split idx at the widest empty gap along one axis: (before, after), or None if no gap."""
    order = sorted(idx, key=start)
    best: tuple[int, int] | None = None  # (gap width, split position)
    reach = end(order[0])
    for pos in range(1, len(order)):
        s = start(order[pos])
        if s >= reach and (best is None or s - reach > best[0]):
            best = (s - reach, pos)
        reach = max(reach, end(order[pos]))
    if best is None:
        return None
    return order[: best[1]], order[best[1] :]


def _xy_cut(boxes: Sequence[BBox], idx: list[int], direction: Direction) -> list[int]:
    if len(idx) <= 1:
        return idx
    rows = _best_cut(idx, lambda i: boxes[i].y, lambda i: boxes[i].y + boxes[i].h)
    if rows is not None:
        top, bottom = rows
        return _xy_cut(boxes, top, direction) + _xy_cut(boxes, bottom, direction)
    cols = _best_cut(idx, lambda i: boxes[i].x, lambda i: boxes[i].x + boxes[i].w)
    if cols is not None:
        left, right = cols
        first, second = (right, left) if direction == "rtl" else (left, right)
        return _xy_cut(boxes, first, direction) + _xy_cut(boxes, second, direction)
    # Overlapping boxes: no cut exists. Rows of 1/20 of the group's height span, then by edge.
    y_min = min(boxes[i].y for i in idx)
    span = max(boxes[i].y + boxes[i].h for i in idx) - y_min
    band = span / FALLBACK_BANDS

    def key(i: int) -> tuple[int, int]:
        b = boxes[i]
        row = int((b.y - y_min) // band)
        return (row, -(b.x + b.w)) if direction == "rtl" else (row, b.x)

    return sorted(idx, key=key)


def reading_order(boxes: Sequence[BBox], direction: Direction) -> list[int]:
    """Recursive XY-cut. Returns a permutation of ``range(len(boxes))`` in reading order.

    At each level the widest horizontal gap splits top from bottom; failing that, the widest
    vertical gap splits columns (right first for ``rtl``). Boxes that touch count as separable.
    """
    if direction not in ("rtl", "ltr"):
        raise ValueError("direction must be 'rtl' or 'ltr'")
    return _xy_cut(boxes, list(range(len(boxes))), direction)


# ---------------------------------------------------------------------------------------------
# manhwa strips


class Slice(NamedTuple):
    """Rows [y0, y1) of a strip; ``hard_cut_after`` when the cut at y1 found no gutter (so a
    panel or balloon may be split across this slice and the next)."""

    y0: int
    y1: int
    hard_cut_after: bool


def slice_strip_cuts(img: npt.NDArray[np.generic], max_height: int = 3000) -> list[Slice]:
    """Cut a vertical strip into contiguous slices no taller than ``max_height``.

    Each cut goes in the middle of the widest all-white row run inside the window (a run that
    begins at the window's top is the gutter just cut through and is skipped). With no gutter
    the cut is a hard one at ``max_height``, and the slice before it says so.
    """
    if max_height < 1:
        raise ValueError("max_height must be positive")
    gray = _gray(img)
    total = gray.shape[0]
    white = ~_ink(gray).any(axis=1)

    # all-white row runs as [start, end)
    padded = np.concatenate([[False], white, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    runs = list(zip(edges[0::2].tolist(), edges[1::2].tolist(), strict=True))

    slices: list[Slice] = []
    y0 = 0
    while total - y0 > max_height:
        limit = y0 + max_height
        best: tuple[int, int] | None = None  # (clipped width, cut)
        for s, e in runs:
            if s <= y0 or s >= limit:
                continue
            ce = min(e, limit)
            cut = min((s + e) // 2, limit)
            if best is None or ce - s > best[0]:
                best = (ce - s, cut)
        y1 = best[1] if best is not None else limit
        slices.append(Slice(y0, y1, hard_cut_after=best is None))
        y0 = y1
    slices.append(Slice(y0, total, hard_cut_after=False))
    return slices


def slice_strip(img: npt.NDArray[np.generic], max_height: int = 3000) -> list[tuple[int, int]]:
    """``slice_strip_cuts`` as plain [y0, y1) ranges."""
    return [(c.y0, c.y1) for c in slice_strip_cuts(img, max_height)]
