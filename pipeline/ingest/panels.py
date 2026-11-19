"""Classical panel detection, XY-cut reading order and manhwa strip slicing.

``detect_panels`` finds panels on one page: Otsu ink mask, 3x3 dilation to close hairline breaks,
white flooded in from the page border is the gutter, and every remaining connected component
covering at least ``MIN_PANEL_FRACTION`` of the page is a panel. No panel, or one covering at
least ``FULL_PAGE_FRACTION``, means the page is a single full-page panel (full-bleed art).

``reading_order`` is pure (boxes only, no image) so it also orders regions inside a panel.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

import cv2
import numpy as np
import numpy.typing as npt

from pipeline.common.schemas import BBox

Direction = Literal["rtl", "ltr"]

MIN_PANEL_FRACTION = 0.02
FULL_PAGE_FRACTION = 0.90
FALLBACK_BANDS = 20  # overlap fallback: rows are 1/20 of the boxes' height span
# Gutters are paper: a pixel darker than this is never gutter, whatever Otsu says. Without the
# floor, Otsu on a full-bleed mid-grey page calls the grey "white" and the dark art a panel.
PAPER_MIN = 160

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


def detect_panels(img: npt.NDArray[np.generic]) -> list[BBox]:
    gray = _gray(img)
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
    boxes: list[BBox] = []
    for i in range(1, n):
        x, y, bw, bh, px = (int(v) for v in stats[i])
        if px >= FULL_PAGE_FRACTION * area:
            return [page]
        if px >= MIN_PANEL_FRACTION * area:
            boxes.append(BBox(x=x, y=y, w=bw, h=bh))
    return boxes or [page]


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


def slice_strip(img: npt.NDArray[np.generic], max_height: int = 3000) -> list[tuple[int, int]]:
    """Cut a vertical strip into contiguous [y0, y1) ranges no taller than ``max_height``.

    Each cut goes in the middle of the widest all-white row run inside the window (a run that
    begins at the window's top is the gutter just cut through and is skipped). With no gutter
    the cut is a hard one at ``max_height``.
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

    ranges: list[tuple[int, int]] = []
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
        ranges.append((y0, y1))
        y0 = y1
    ranges.append((y0, total))
    return ranges
