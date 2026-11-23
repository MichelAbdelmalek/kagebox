"""Offline understand report for one chapter: ``report.json`` and contact sheets.

Reads the stored results (Postgres + object store) and writes, into ``--out``:

* ``report.json``: per-field null rates, figures per panel, review items by kind, scenes, cached
  and failed counts, seconds per panel (p50/p95, from scene timestamps), the wall time from the
  stage row, the trial projection, and per-panel detail. The scene text is untrusted model
  output: it is written there as JSON strings only;
* ``scenes-NN.png``: each panel crop beside its scene fields. The sheet has no Japanese font and
  none is downloaded, so text the default font cannot draw is replaced by a pointer to
  ``report.json``. With ``--masked`` the crop is the MASKED image the model was sent (needs the
  detector model); without it the stored crop is drawn.

``--mask-sheet`` instead writes ``mask-NN.png``: original beside masked for chosen panels, to
judge the text detector by eye. Everything lands under ``data/reports/`` (gitignored); panel
pixels never go into git or logs. Nothing here writes to the database or the object store.

Run inside the stack network, e.g. through ``deploy/scripts/bakeoff.sh``-style bind mounts::

    python -m orchestration.understand_report CHAPTER_ID --out /reports/NAME [--masked]
"""

from __future__ import annotations

import argparse
import io
import json
import math
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from pipeline.common.dbmodels import (
    PageRow,
    PanelRow,
    ReviewItemRow,
    SceneRow,
    StageRunRow,
    aware,
)
from pipeline.common.schemas import Panel, SceneJSON
from pipeline.common.storage import ObjectStore
from pipeline.understand.contract import REVIEW_KINDS

SCENE_FIELDS = ("setting", "action", "shot_hint", "time_of_day", "weather")
CELL_W, CELL_H, CROP_H = 760, 300, 280
CELLS_PER_SHEET = 6  # 2 columns x 3 rows
NON_LATIN = "[non-latin text: see report.json]"

Masker = Callable[[str, bytes], bytes]  # (panel_id, stored crop png) -> png to draw


# ---------------------------------------------------------------------------------------------
# pure parts (unit-tested)


def null_rates(scenes: Sequence[SceneJSON]) -> dict[str, dict[str, Any]]:
    """Per scene field: how many scenes left it null (no visual evidence)."""
    out: dict[str, dict[str, Any]] = {}
    for name in SCENE_FIELDS:
        null = sum(1 for s in scenes if not getattr(s, name).known)
        total = len(scenes)
        out[name] = {
            "null": null,
            "total": total,
            "null_pct": round(100.0 * null / total, 2) if total else 0.0,
        }
    return out


def figures_summary(scenes: Sequence[SceneJSON]) -> dict[str, Any]:
    counts = [len(s.figures) for s in scenes]
    emotions = [f.emotion.known for s in scenes for f in s.figures]
    return {
        "per_panel": counts,
        "histogram": {str(k): v for k, v in sorted(Counter(counts).items())},
        "mean": round(sum(counts) / len(counts), 3) if counts else None,
        "emotion_null_pct": (
            round(100.0 * sum(1 for e in emotions if not e) / len(emotions), 2)
            if emotions
            else None
        ),
    }


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def panel_seconds(created: Sequence[float], started: float, load_s: float) -> list[float]:
    """Seconds per described panel from consecutive scene timestamps (epoch seconds).

    The first panel is measured from the stage start minus the model load; later ones are the
    gap to the previous scene. A panel that failed or came from cache between two scenes widens
    that gap a little, so this is a distribution estimate; the flow's own mean is exact.
    """
    out: list[float] = []
    previous = started + load_s
    for t in created:
        out.append(round(max(0.0, t - previous), 3))
        previous = t
    return out


def ascii_line(value: str | None, width: int = 70) -> str:
    """One drawable line for the sheet: the default PIL font has no Japanese glyphs."""
    if value is None:
        return "null"
    flat = " ".join(value.split())
    if not flat.isascii():
        return NON_LATIN
    return flat[:width]


# ---------------------------------------------------------------------------------------------
# reading the stored run


def _epoch(dt: datetime) -> float:
    return aware(dt).timestamp()


def _stage(row: StageRunRow | None) -> dict[str, Any]:
    if row is None:
        return {"status": None, "trial": None}
    counts = dict(row.counts or {})
    wall = None
    if row.started_at is not None and row.finished_at is not None:
        wall = round(_epoch(row.finished_at) - _epoch(row.started_at), 1)
    return {"status": row.status, "error": row.error, "wall_time_s": wall, **counts}


def _fields(scene: SceneJSON) -> dict[str, Any]:
    d: dict[str, Any] = {n: getattr(scene, n).value for n in SCENE_FIELDS}
    d["figures"] = [
        {"description": f.description.value, "emotion": f.emotion.value} for f in scene.figures
    ]
    return d


def build_report(
    engine: Engine,
    store: ObjectStore,
    chapter_id: str,
    out: Path,
    masker: Masker | None = None,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    with Session(engine) as s:
        rows = list(
            s.execute(
                select(PanelRow, PageRow.index)
                .join(PageRow, PanelRow.page_id == PageRow.id)
                .where(PanelRow.chapter_id == chapter_id)
                .order_by(PageRow.index, PanelRow.reading_order)
            )
        )
        scene_rows = {
            r.panel_id: r
            for r in s.scalars(select(SceneRow).where(SceneRow.chapter_id == chapter_id))
        }
        reviews = list(
            s.scalars(
                select(ReviewItemRow).where(
                    ReviewItemRow.chapter_id == chapter_id,
                    ReviewItemRow.kind.in_(REVIEW_KINDS),
                    ReviewItemRow.status == "open",
                )
            )
        )
        stage_row = s.get(StageRunRow, (chapter_id, "understand"))
        stage = _stage(stage_row)
        started = stage_row.started_at if stage_row is not None else None

    kinds_by_panel: dict[str, list[str]] = {}
    for r in reviews:
        kinds_by_panel.setdefault(r.artifact_id, []).append(r.kind)
    scenes: dict[str, SceneJSON] = {}
    created: list[tuple[float, str]] = []
    for panel_row, _ in rows:
        sr = scene_rows.get(panel_row.id)
        if sr is None:
            continue
        scenes[panel_row.id] = SceneJSON.model_validate_json(json.dumps(sr.artifact))
        created.append((_epoch(sr.created_at), panel_row.id))
    ordered = [scenes[p.id] for p, _ in rows if p.id in scenes]

    # seconds per panel: only scenes written by the latest run (a cached scene is older)
    run_start = _epoch(started) if started is not None else None
    fresh = sorted(t for t, _ in created if run_start is None or t >= run_start)
    secs = panel_seconds(fresh, run_start or 0.0, float(stage.get("load_s") or 0.0))
    pace: dict[str, Any] = {
        "measured_panels": stage.get("measured_panels", len(secs)),
        "mean_s": stage.get("seconds_per_panel")
        or (round(sum(secs) / len(secs), 3) if secs else None),
        "p50_s": percentile(secs, 50),
        "p95_s": percentile(secs, 95),
        "max_s": max(secs) if secs else None,
        "per_panel_s": secs,
    }

    detail: list[dict[str, Any]] = []
    for panel_row, page_index in rows:
        sc = scenes.get(panel_row.id)
        detail.append(
            {
                "panel_id": panel_row.id,
                "page": page_index,
                "order": panel_row.reading_order,
                "scene": None if sc is None else _fields(sc),
                "review_kinds": sorted(kinds_by_panel.get(panel_row.id, [])),
            }
        )

    sheets = _contact_sheets(store, rows, detail, out, masker)
    kinds = Counter(r.kind for r in reviews)
    report: dict[str, Any] = {
        "chapter_id": chapter_id,
        "panels": len(rows),
        "scenes": len(scenes),
        "cached": stage.get("cached"),
        "failed": stage.get(
            "failed", sum(1 for k in kinds_by_panel.values() if "scene_failed" in k)
        ),
        "review_items_by_kind": dict(sorted(kinds.items())),
        "null_rates": null_rates(ordered),
        "figures": figures_summary(ordered),
        "seconds_per_panel": pace,
        "wall_time_s": stage.get("wall_time_s"),
        "stage": stage,
        "masked_sheets": masker is not None,
        "sheets": sheets,
        "panels_detail": detail,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report


# ---------------------------------------------------------------------------------------------
# drawing (needs the stack)


def _font(size: int) -> Any:
    return ImageFont.load_default(size)


def _cell(png: bytes, item: dict[str, Any]) -> Image.Image:
    cell = Image.new("RGB", (CELL_W, CELL_H), "white")
    draw = ImageDraw.Draw(cell)
    with Image.open(io.BytesIO(png)) as src:
        crop = src.convert("RGB")
    crop.thumbnail((CELL_W // 2 - 20, CROP_H))
    cell.paste(crop, (10, 10))
    x, y = CELL_W // 2, 10
    head = f"page {item['page']} panel {item['order']}"
    draw.text((x, y), head, fill=(20, 80, 230), font=_font(18))
    y += 26
    scene = item["scene"]
    if scene is None:
        draw.text((x, y), "no scene (failed or not run)", fill=(230, 0, 0), font=_font(15))
    else:
        for name in SCENE_FIELDS:
            draw.text((x, y), f"{name}: {ascii_line(scene[name], 52)}", fill=0, font=_font(14))
            y += 19
        draw.text((x, y), f"figures: {len(scene['figures'])}", fill=0, font=_font(14))
        y += 19
        for f in scene["figures"][:4]:
            draw.text(
                (x + 8, y),
                "- " + ascii_line(f["description"], 56),
                fill=(60, 60, 60),
                font=_font(13),
            )
            y += 17
    if item["review_kinds"]:
        draw.text(
            (x, CELL_H - 24),
            "review: " + ", ".join(item["review_kinds"]),
            fill=(230, 0, 0),
            font=_font(14),
        )
    draw.rectangle((0, 0, CELL_W - 1, CELL_H - 1), outline=(180, 180, 180))
    return cell


def _contact_sheets(
    store: ObjectStore,
    rows: Sequence[Any],
    detail: Sequence[dict[str, Any]],
    out: Path,
    masker: Masker | None,
) -> list[str]:
    shown = [
        (r, d) for (r, _), d in zip(rows, detail, strict=True) if d["scene"] or d["review_kinds"]
    ]
    names: list[str] = []
    for n, start in enumerate(range(0, len(shown), CELLS_PER_SHEET), 1):
        chunk = shown[start : start + CELLS_PER_SHEET]
        sheet = Image.new("RGB", (CELL_W * 2, CELL_H * 3), "white")
        for k, (row, item) in enumerate(chunk):
            panel = Panel.model_validate_json(json.dumps(row.artifact))
            png = store.get(panel.crop_ref)
            if masker is not None:
                png = masker(row.id, png)
            sheet.paste(_cell(png, item), ((k % 2) * CELL_W, (k // 2) * CELL_H))
        name = f"scenes-{n:02d}.png"
        sheet.save(out / name)
        names.append(name)
    return names


# ---------------------------------------------------------------------------------------------
# detector check sheets


def pick_panels(
    ordered: Sequence[str], trial: int, extra: int, forced: Sequence[str] = ()
) -> list[str]:
    """The first ``trial`` panels, the ``forced`` ones, then ``extra`` evenly spaced others."""
    chosen = list(ordered[:trial])
    for pid in forced:
        if pid not in chosen:
            chosen.append(pid)
    rest = [p for p in ordered if p not in chosen]
    if extra > 0 and rest:
        step = len(rest) / extra
        for i in range(min(extra, len(rest))):
            chosen.append(rest[int(i * step)])
    return chosen


def _mask_sheets(pairs: Sequence[tuple[str, bytes, bytes]], out: Path) -> list[str]:
    names: list[str] = []
    per = 3
    for n, start in enumerate(range(0, len(pairs), per), 1):
        chunk = pairs[start : start + per]
        tiles: list[Image.Image] = []
        for label, orig, masked in chunk:
            ims = []
            for png in (orig, masked):
                with Image.open(io.BytesIO(png)) as src:
                    im = src.convert("RGB")
                im.thumbnail((700, 520))
                ims.append(im)
            h = max(i.height for i in ims) + 28
            tile = Image.new("RGB", (1420, h), "white")
            ImageDraw.Draw(tile).text((6, 4), label, fill=(20, 80, 230), font=_font(18))
            tile.paste(ims[0], (0, 28))
            tile.paste(ims[1], (710, 28))
            tiles.append(tile)
        sheet = Image.new("RGB", (1420, sum(t.height for t in tiles)), "white")
        y = 0
        for t in tiles:
            sheet.paste(t, (0, y))
            y += t.height
        name = f"mask-{n:02d}.png"
        sheet.save(out / name)
        names.append(name)
    return names


def mask_review(
    engine: Engine,
    store: ObjectStore,
    chapter_id: str,
    out: Path,
    trial: int,
    extra: int,
    forced_source_pages: Sequence[int],
) -> dict[str, Any]:
    """Original beside masked (real detector, current settings) for trial panels + others."""
    from orchestration import understand_flow as uf
    from pipeline.ingest.bubbles import BubbleDetector
    from pipeline.understand.request import prepare_panel
    from pipeline.understand.settings import load_settings

    out.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    detector = BubbleDetector()
    work = uf._load_panels(engine, chapter_id)
    with Session(engine) as s:
        page_of = {
            pid: (idx, art["source_page"])
            for pid, idx, art in s.execute(
                select(PanelRow.id, PageRow.index, PageRow.artifact).join(
                    PageRow, PanelRow.page_id == PageRow.id
                )
            )
            if pid in {w.id for w in work}
        }
    forced: list[str] = []
    for src_page in forced_source_pages:
        forced.extend(w.id for w in work if page_of[w.id][1] == src_page)
    ids = pick_panels([w.id for w in work], trial, extra, forced)
    by_id = {w.id: w for w in work}
    pairs: list[tuple[str, bytes, bytes]] = []
    manifest: list[dict[str, Any]] = []
    for pid in ids:
        w = by_id[pid]
        crop = store.get(w.panel.crop_ref)
        prepared = prepare_panel(crop, w.panel.bbox, w.bubbles, w.page_boxes, detector, settings)
        idx, src = page_of[pid]
        pairs.append((f"page {idx} (pdf p{src}) panel {w.panel.reading_order}", crop, prepared.png))
        manifest.append(
            {
                "panel_id": pid,
                "page": idx,
                "source_page": src,
                "masked_sha256": prepared.input_hashes[0],
            }
        )
    sheets = _mask_sheets(pairs, out)
    result = {"sheets": sheets, "panels": manifest}
    (out / "mask-review.json").write_text(json.dumps(result, indent=2), "utf-8")
    return result


# ---------------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chapter_id")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--masked", action="store_true", help="draw the masked crops (needs the model)")
    ap.add_argument("--mask-sheet", action="store_true", help="write the detector check sheets")
    ap.add_argument("--trial", type=int, default=10, help="mask sheet: leading panels to include")
    ap.add_argument("--extra", type=int, default=10, help="mask sheet: evenly spaced others")
    ap.add_argument("--source-pages", default="", help="mask sheet: PDF pages to include, 5,7,20")
    args = ap.parse_args(argv)

    from pipeline.common.database import make_engine
    from pipeline.common.settings import get_settings
    from pipeline.common.storage import make_store

    app = get_settings()
    engine, store = make_engine(app), make_store(app)
    if args.mask_sheet:
        pages = [int(p) for p in args.source_pages.split(",") if p]
        res = mask_review(engine, store, args.chapter_id, args.out, args.trial, args.extra, pages)
        print(json.dumps({"sheets": res["sheets"], "panels": len(res["panels"])}))
        return
    masker: Masker | None = None
    if args.masked:
        from orchestration import understand_flow as uf
        from pipeline.ingest.bubbles import BubbleDetector
        from pipeline.understand.request import prepare_panel
        from pipeline.understand.settings import load_settings

        settings, detector = load_settings(), BubbleDetector()
        work = {w.id: w for w in uf._load_panels(engine, args.chapter_id)}

        def masker(pid: str, png: bytes) -> bytes:
            w = work[pid]
            return prepare_panel(png, w.panel.bbox, w.bubbles, w.page_boxes, detector, settings).png

    report = build_report(engine, store, args.chapter_id, args.out, masker)
    keys = (
        "panels",
        "scenes",
        "cached",
        "failed",
        "review_items_by_kind",
        "null_rates",
        "figures",
        "wall_time_s",
    )
    print(
        json.dumps(
            {k: report[k] for k in keys}
            | {
                "pace": {k: v for k, v in report["seconds_per_panel"].items() if k != "per_panel_s"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
