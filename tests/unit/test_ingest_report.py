"""Pure parts of the offline ingest report: histogram, aggregation, threshold sweep."""

from __future__ import annotations

import pytest

from orchestration.ingest_report import (
    PageStats,
    histogram,
    irregular_sweep,
    reason_bbox,
    summarize,
)
from pipeline.common.schemas import BBox


def test_histogram_bins_cover_range_and_clamp_edges() -> None:
    bins = histogram([0.0, 0.05, 0.5, 0.95, 1.0], lo=0.0, hi=1.0, n=10)
    assert len(bins) == 10
    assert bins[0] == {"lo": 0.0, "hi": 0.1, "count": 2}
    assert bins[5]["count"] == 1
    assert bins[9] == {"lo": 0.9, "hi": 1.0, "count": 2}  # 1.0 lands in the last bin
    assert sum(b["count"] for b in bins) == 5


def test_histogram_clamps_out_of_range_values() -> None:
    bins = histogram([-1.0, 2.0], lo=0.0, hi=1.0, n=4)
    assert [b["count"] for b in bins] == [1, 0, 0, 1]


def test_histogram_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError):
        histogram([0.5], lo=1.0, hi=1.0, n=4)
    with pytest.raises(ValueError):
        histogram([0.5], lo=0.0, hi=1.0, n=0)


def _page(
    index: int, panels: int, conf: list[float], flagged: int, kinds: dict[str, int]
) -> PageStats:
    return PageStats(
        index=index,
        source_page=index + 2,
        panels=panels,
        bubbles=len(conf),
        bubbles_needs_review=flagged,
        confidences=conf,
        review_kinds=kinds,
    )


def test_summarize_totals_and_rates() -> None:
    pages = [
        _page(1, 1, [0.99, 0.5], 1, {"panel_full_page": 1, "ocr_low_confidence": 1}),
        _page(2, 4, [0.95, 0.97], 0, {}),
        _page(3, 3, [], 0, {"panel_irregular": 2}),
    ]
    s = summarize(pages)
    assert s["pages"] == 3
    assert s["panels"] == 8
    assert s["bubbles"] == 4
    assert s["bubbles_needs_review"] == 1
    assert s["bubbles_flagged_pct"] == 25.0
    assert s["pages_with_review_items"] == 2
    assert s["pages_flagged_pct"] == pytest.approx(66.67, abs=0.01)
    assert s["review_items"] == 4
    assert s["review_items_by_kind"] == {
        "ocr_low_confidence": 1,
        "panel_full_page": 1,
        "panel_irregular": 2,
    }
    assert s["ocr_confidence_mean"] == pytest.approx((0.99 + 0.5 + 0.95 + 0.97) / 4)
    assert s["ocr_confidence_min"] == 0.5
    assert sum(b["count"] for b in s["ocr_confidence_histogram"]) == 4


def test_summarize_empty_chapter_has_no_division_by_zero() -> None:
    s = summarize([])
    assert s["pages"] == 0
    assert s["bubbles_flagged_pct"] == 0.0
    assert s["pages_flagged_pct"] == 0.0
    assert s["ocr_confidence_mean"] is None
    assert s["ocr_confidence_min"] is None


def test_irregular_sweep_counts_fills_below_each_threshold() -> None:
    fills = [0.40, 0.70, 0.80, 0.84, 0.86, 0.99]
    assert irregular_sweep(fills, [0.70, 0.80, 0.85]) == {"0.70": 1, "0.80": 2, "0.85": 4}


def test_reason_bbox_parses_the_flow_format_only() -> None:
    assert reason_bbox("speech region at x=10 y=20 w=30 h=40 overlaps no panel") == BBox(
        x=10, y=20, w=30, h=40
    )
    assert reason_bbox("single whole-page panel (full_page); confirm") is None
    assert reason_bbox("x=1 y=2 w=0 h=4") is None  # not a valid box
