"""Offline ingest report for one chapter: contact sheets and ``report.json``.

Reads the chapter's stored results (Postgres + object store) and writes, into ``--out``:

* ``page-NNN.png``: each page with panel boxes and reading-order numbers (blue), bubble boxes
  coloured by kind with ``panel.bubble`` order labels, bubbles that need review outlined in red,
  and regions outside every panel (from ``region_outside_panels`` review items) in magenta;
* ``sheet-NN.png``: grids of those pages, ``SHEET_COLS`` x ``SHEET_ROWS`` per sheet;
* ``report.json``: per-page counts, review items by kind, OCR confidence histogram, the panel
  fill distribution (re-measured with ``find_panels`` on the stored page images, for
  calibrating ``panel_irregular_fill``) and the wall time passed in by the caller.

OCR text is untrusted data: it is written to ``report.json`` as JSON strings only. It is drawn
on the sheets only when a font that can render it is already installed (``--font``); no font
is downloaded. Nothing here writes to the database or the object store.

Run inside the stack network (it needs the database and object store), e.g.::

    docker compose run --rm --no-deps -v ../data/reports:/reports worker-cpu \
        python -m orchestration.ingest_report CHAPTER_ID --out /reports/NAME --wall-time-s 812
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.common.schemas import BBox
from pipeline.common.schemas.source import OCR_CONFIDENCE_MIN

PAGE_HEIGHT = 1400  # annotated page images are scaled to this height
SHEET_COLS, SHEET_ROWS = 4, 2
SWEEP = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85)
KIND_COLOURS = {
    "speech": (0, 160, 0),
    "caption": (230, 140, 0),
    "thought": (0, 170, 170),
    "sfx": (120, 60, 200),
}
PANEL_COLOUR = (20, 80, 230)
REVIEW_COLOUR = (230, 0, 0)
OUTSIDE_COLOUR = (220, 0, 220)
_BOX = re.compile(r"x=(\d+) y=(\d+) w=(\d+) h=(\d+)")


# ---------------------------------------------------------------------------------------------
# pure parts (unit-tested)


@dataclass
class PageStats:
    index: int
    source_page: int
    panels: int
    bubbles: int
    bubbles_needs_review: int
    confidences: list[float] = field(default_factory=list)
    review_kinds: dict[str, int] = field(default_factory=dict)


def histogram(values: Iterable[float], lo: float, hi: float, n: int) -> list[dict[str, Any]]:
    """``n`` equal bins over [lo, hi]; values outside are clamped into the end bins."""
    if n < 1 or hi <= lo:
        raise ValueError("need n >= 1 and hi > lo")
    width = (hi - lo) / n
    counts = [0] * n
    for v in values:
        i = math.floor((v - lo) / width)
        counts[min(max(i, 0), n - 1)] += 1
    return [
        {"lo": round(lo + k * width, 6), "hi": round(lo + (k + 1) * width, 6), "count": c}
        for k, c in enumerate(counts)
    ]


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def summarize(pages: Sequence[PageStats]) -> dict[str, Any]:
    """Chapter totals from per-page stats."""
    confs = [c for p in pages for c in p.confidences]
    kinds: Counter[str] = Counter()
    for p in pages:
        kinds.update(p.review_kinds)
    bubbles = sum(p.bubbles for p in pages)
    needs_review = sum(p.bubbles_needs_review for p in pages)
    flagged_pages = sum(1 for p in pages if sum(p.review_kinds.values()) > 0)
    return {
        "pages": len(pages),
        "panels": sum(p.panels for p in pages),
        "bubbles": bubbles,
        "bubbles_needs_review": needs_review,
        "bubbles_flagged_pct": _pct(needs_review, bubbles),
        "pages_with_review_items": flagged_pages,
        "pages_flagged_pct": _pct(flagged_pages, len(pages)),
        "review_items": sum(kinds.values()),
        "review_items_by_kind": dict(sorted(kinds.items())),
        "ocr_confidence_min_threshold": OCR_CONFIDENCE_MIN,
        "ocr_confidence_mean": sum(confs) / len(confs) if confs else None,
        "ocr_confidence_min": min(confs) if confs else None,
        "ocr_confidence_histogram": histogram(confs, 0.0, 1.0, 20),
    }


def irregular_sweep(fills: Iterable[float], thresholds: Iterable[float]) -> dict[str, int]:
    """How many component panels would be flagged ``panel_irregular`` at each threshold."""
    values = list(fills)
    return {f"{t:.2f}": sum(1 for f in values if f < t) for t in thresholds}


def reason_bbox(reason: str) -> BBox | None:
    """The box in a review reason written by the ingest flow (``x=.. y=.. w=.. h=..``)."""
    m = _BOX.search(reason)
    if m is None:
        return None
    x, y, w, h = (int(v) for v in m.groups())
    if w <= 0 or h <= 0:
        return None
    return BBox(x=x, y=y, w=w, h=h)


# ---------------------------------------------------------------------------------------------
# drawing and I/O (needs the stack)


def _font(path: str | None, size: int) -> Any:
    from PIL import ImageFont

    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _annotate(
    page_png: bytes,
    panels: list[dict[str, Any]],
    outside: list[BBox],
    text_font: str | None,
) -> Any:
    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(page_png)) as src:
        img = src.convert("RGB")
    scale = PAGE_HEIGHT / img.height
    img = img.resize((max(1, round(img.width * scale)), PAGE_HEIGHT))
    draw = ImageDraw.Draw(img)
    big, small = _font(None, 44), _font(None, 20)
    ja = _font(text_font, 18) if text_font else None

    def box(b: BBox) -> tuple[int, int, int, int]:
        return (
            round(b.x * scale),
            round(b.y * scale),
            round((b.x + b.w) * scale),
            round((b.y + b.h) * scale),
        )

    for p in panels:
        x0, y0, x1, y1 = box(p["bbox"])
        draw.rectangle((x0, y0, x1, y1), outline=PANEL_COLOUR, width=5)
        draw.text((x0 + 8, y0 + 4), str(p["order"]), fill=PANEL_COLOUR, font=big)
        for b in p["bubbles"]:
            bx0, by0, bx1, by1 = box(b["bbox"])
            colour = KIND_COLOURS.get(b["kind"], (0, 0, 0))
            draw.rectangle((bx0, by0, bx1, by1), outline=colour, width=3)
            if b["needs_review"]:
                draw.rectangle((bx0 - 5, by0 - 5, bx1 + 5, by1 + 5), outline=REVIEW_COLOUR, width=3)
            label = f"{p['order']}.{b['order']}"
            draw.rectangle((bx0, by0, bx0 + 12 * len(label) + 6, by0 + 22), fill=colour)
            draw.text((bx0 + 3, by0 + 1), label, fill=(255, 255, 255), font=small)
            if ja is not None and b["text"]:
                draw.text((bx0, by1 + 2), b["text"][:40], fill=colour, font=ja)
    for b in outside:
        draw.rectangle(box(b), outline=OUTSIDE_COLOUR, width=4)
    return img


def _sheets(images: list[tuple[int, Any]], out: Path) -> list[str]:
    from PIL import Image, ImageDraw

    per = SHEET_COLS * SHEET_ROWS
    names: list[str] = []
    for s in range(0, len(images), per):
        chunk = images[s : s + per]
        cw = max(im.width for _, im in chunk) // 2
        ch = PAGE_HEIGHT // 2 + 30
        sheet = Image.new("RGB", (cw * SHEET_COLS, ch * SHEET_ROWS), "white")
        draw = ImageDraw.Draw(sheet)
        for k, (index, im) in enumerate(chunk):
            col, row = SHEET_COLS - 1 - k % SHEET_COLS, k // SHEET_COLS  # right to left
            thumb = im.resize((im.width // 2, im.height // 2))
            sheet.paste(thumb, (col * cw, row * ch + 30))
            draw.text(
                (col * cw + 6, row * ch + 4), f"page {index}", fill=(0, 0, 0), font=_font(None, 22)
            )
        name = f"sheet-{s // per + 1:02d}.png"
        sheet.save(out / name)
        names.append(name)
    return names


def build_report(
    chapter_id: str, out: Path, wall_time_s: float | None, text_font: str | None
) -> dict[str, Any]:
    import numpy as np
    from PIL import Image
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from pipeline.common.database import make_engine
    from pipeline.common.dbmodels import BubbleRow, ChapterRow, PageRow, PanelRow, ReviewItemRow
    from pipeline.common.settings import get_settings
    from pipeline.common.storage import make_store
    from pipeline.ingest.panels import find_panels

    settings = get_settings()
    engine, store = make_engine(settings), make_store(settings)
    out.mkdir(parents=True, exist_ok=True)
    with Session(engine) as s:
        chapter = s.get(ChapterRow, chapter_id)
        if chapter is None:
            raise LookupError("chapter not found")
        pages = list(
            s.scalars(
                select(PageRow).where(PageRow.chapter_id == chapter_id).order_by(PageRow.index)
            )
        )
        panels = list(s.scalars(select(PanelRow).where(PanelRow.chapter_id == chapter_id)))
        bubbles = list(s.scalars(select(BubbleRow).where(BubbleRow.chapter_id == chapter_id)))
        reviews = list(
            s.scalars(select(ReviewItemRow).where(ReviewItemRow.chapter_id == chapter_id))
        )
    engine.dispose()

    page_of_panel = {p.id: p.page_id for p in panels}
    page_of_bubble = {b.id: page_of_panel[b.panel_id] for b in bubbles}
    review_page = {
        r.id: page_of_panel.get(r.artifact_id) or page_of_bubble.get(r.artifact_id) or r.artifact_id
        for r in reviews
    }

    stats: list[PageStats] = []
    page_rows: list[dict[str, Any]] = []
    fills: list[float] = []
    images: list[tuple[int, Any]] = []
    for page in pages:
        art = page.artifact
        p_panels = sorted(
            (p for p in panels if p.page_id == page.id), key=lambda p: p.reading_order
        )
        drawn: list[dict[str, Any]] = []
        confs: list[float] = []
        needs = 0
        texts: list[dict[str, Any]] = []
        for p in p_panels:
            p_bubbles = sorted(
                (b for b in bubbles if b.panel_id == p.id), key=lambda b: b.reading_order
            )
            boxed: list[dict[str, Any]] = []
            entry = {
                "order": p.reading_order,
                "bbox": BBox.model_validate(p.artifact["bbox"]),
                "bubbles": boxed,
            }
            for b in p_bubbles:
                conf = float(b.artifact["ocr_confidence"])
                confs.append(conf)
                needs += int(b.needs_review)
                boxed.append(
                    {
                        "order": b.reading_order,
                        "bbox": BBox.model_validate(b.artifact["bbox"]),
                        "kind": b.artifact["kind"],
                        "needs_review": b.needs_review,
                        "text": b.artifact["text"],
                    }
                )
                texts.append(
                    {
                        "panel": p.reading_order,
                        "bubble": b.reading_order,
                        "kind": b.artifact["kind"],
                        "confidence": round(conf, 4),
                        "needs_review": b.needs_review,
                        "text": b.artifact["text"],  # untrusted OCR output: data only
                    }
                )
            drawn.append(entry)
        p_reviews = [r for r in reviews if review_page[r.id] == page.id]
        kinds = Counter(r.kind for r in p_reviews)
        outside = [
            box
            for r in p_reviews
            if r.kind == "region_outside_panels" and (box := reason_bbox(r.reason)) is not None
        ]
        png = store.get(art["image_ref"])
        with Image.open(io.BytesIO(png)) as im:
            cands = find_panels(np.asarray(im.convert("RGB")))
        page_fills = [round(c.fill, 4) for c in cands if c.reason == "component"]
        fills.extend(page_fills)
        img = _annotate(png, drawn, outside, text_font)
        img.save(out / f"page-{page.index:03d}.png")
        images.append((page.index, img))
        st = PageStats(
            index=page.index,
            source_page=art["source_page"],
            panels=len(p_panels),
            bubbles=len(confs),
            bubbles_needs_review=needs,
            confidences=confs,
            review_kinds=dict(kinds),
        )
        stats.append(st)
        page_rows.append(
            {
                "index": page.index,
                "source_page": art["source_page"],
                "size": [art["width"], art["height"]],
                "panels": st.panels,
                "bubbles": st.bubbles,
                "bubbles_needs_review": needs,
                "review_items": dict(sorted(kinds.items())),
                "review_reasons": sorted(f"{r.kind}: {r.reason}" for r in p_reviews),
                "component_panel_fills": page_fills,
                "bubbles_text": texts,
            }
        )

    sheets = _sheets(images, out)
    report = {
        "chapter_id": chapter_id,
        "status": chapter.status,
        "language": chapter.language,
        "format": chapter.format,
        "source": {
            "kind": chapter.source_kind,
            "ref": chapter.source_ref,
            "sha256": chapter.source_sha256,
            "page_start": chapter.page_start,
            "page_end": chapter.page_end,
        },
        "wall_time_s": wall_time_s,
        "summary": summarize(stats),
        "panel_fill": {
            "component_panels": len(fills),
            "histogram": histogram(fills, 0.0, 1.0, 20),
            "flagged_at_threshold": irregular_sweep(fills, SWEEP),
        },
        "text_rendered_on_sheets": text_font is not None,
        "sheets": sheets,
        "pages": page_rows,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chapter_id")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--wall-time-s", type=float, default=None)
    ap.add_argument("--font", default=None, help="an installed font that can render the text")
    args = ap.parse_args(argv)
    report = build_report(args.chapter_id, args.out, args.wall_time_s, args.font)
    print(json.dumps(report["summary"] | {"panel_fill": report["panel_fill"]}, indent=2))


if __name__ == "__main__":
    main()
