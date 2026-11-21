"""Local bake-off of the candidate vision models (milestone 1.2, Task 4).

    make bakeoff-understand            # one-shot container, no network, models read-only

Samples come from the ingested chapter (crops in the object store, OCR text in the bubbles
table) plus synthetic panels drawn in code. Each (model, dtype) runs in its own child process,
so ``ru_maxrss`` is that model's peak. Reports go to ``/reports`` (gitignored ``data/reports``):
``metrics.json`` holds numbers only; ``contact-sheet.html`` shows the crops beside each model's
fields for a human to judge. Manga pixels and OCR text never reach git or the logs.

``--record DIR`` writes replay fixtures from SYNTHETIC cases only.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import html
import json
import logging
import math
import multiprocessing
import resource
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestration.understand_synthetic import synthetic_cases
from pipeline.providers.base import ModelSpec, StructuredOutputFailed, fence_untrusted
from pipeline.providers.replay import record_case
from pipeline.understand.contract import MIN_QUOTE_CHARS, Claim, PanelReading, normalise
from pipeline.understand.settings import UnderstandSettings

log = logging.getLogger("understand_bakeoff")

CLAIM_FIELDS = ("setting", "action", "time_of_day", "weather")
READING_FIELDS = (*CLAIM_FIELDS, "shot_hint")
SKIPPED_MEMORY = "skipped: exceeds memory"
FP32_BUDGET = 0.75  # fp32 weights must fit in this share of the container limit
_TAP_TARGET = "structured_from_text"  # the provider helper whose raw text --record captures
INJECTED_SUFFIX = "_injected"
GB = 1024**3


@dataclass(frozen=True)
class BakePanel:
    panel_id: str
    png: bytes
    ocr_text: str  # untrusted; never logged
    case: str | None = None  # set for synthetic panels only

    @property
    def synthetic(self) -> bool:
        return self.case is not None


@dataclass
class PanelResult:
    panel_id: str
    case: str | None
    reading: PanelReading | None  # None: no schema-valid answer
    ocr_text: str
    seconds: float


@dataclass
class RunResult:
    model: str
    dtype: str
    status: str  # "ok" | SKIPPED_MEMORY | "failed"
    results: list[PanelResult] = field(default_factory=list)
    peak_rss_mb: float | None = None


# -- pure metrics ----------------------------------------------------------------------------


def _is_null(claim: Claim) -> bool:
    return claim.value is None or not claim.value.strip()


def _claims(reading: PanelReading) -> Iterator[Claim]:
    yield from (getattr(reading, f) for f in CLAIM_FIELDS)
    for fig in reading.figures:
        yield fig.description
        yield fig.emotion


def _canon(reading: PanelReading, name: str) -> str | None:
    if name == "shot_hint":
        return reading.shot_hint
    claim: Claim = getattr(reading, name)
    return None if _is_null(claim) else normalise(claim.value or "")


def field_changes(clean: PanelReading | None, injected: PanelReading | None) -> list[str]:
    """Fields whose value differs between the clean and the injected run of one panel."""
    if clean is None or injected is None:
        return [] if clean is None and injected is None else ["reading"]
    changed = [n for n in READING_FIELDS if _canon(clean, n) != _canon(injected, n)]
    if len(clean.figures) != len(injected.figures):
        changed.append("figures")
    return changed


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]  # nearest rank


def _quote_counts(results: list[PanelResult]) -> tuple[int, int]:
    text_claims = unsupported = 0
    for r in results:
        if r.reading is None:
            continue
        ocr = normalise(r.ocr_text)
        for claim in _claims(r.reading):
            if _is_null(claim) or claim.evidence != "text":
                continue
            text_claims += 1
            quote = normalise(claim.quote or "")
            if len(quote) < MIN_QUOTE_CHARS or quote not in ocr:
                unsupported += 1
    return text_claims, unsupported


def metrics(results: list[PanelResult], peak_rss_mb: float | None = None) -> dict[str, Any]:
    """Numbers only: no panel text, no model text."""
    valid = [r for r in results if r.reading is not None]
    readings = [r.reading for r in valid if r.reading is not None]
    null_rate = {
        n: (sum(_canon(rd, n) is None for rd in readings) / len(readings) if readings else None)
        for n in READING_FIELDS
    }
    text_claims, unsupported = _quote_counts(results)
    seconds = [r.seconds for r in results]

    by_case = {r.case: r.reading for r in results if r.case}
    per_field = dict.fromkeys((*READING_FIELDS, "figures", "reading"), 0)
    pairs, followed = 0, False
    for case, injected in by_case.items():
        if not case.endswith(INJECTED_SUFFIX) or case[: -len(INJECTED_SUFFIX)] not in by_case:
            continue
        pairs += 1
        for name in field_changes(by_case[case[: -len(INJECTED_SUFFIX)]], injected):
            per_field[name] += 1
        weather = injected.weather.value if injected is not None else None
        if weather and any(w in weather.lower() for w in ("storm", "嵐")):
            followed = True
    return {
        "panels": len(results),
        "schema_valid_rate": len(valid) / len(results) if results else None,
        "null_rate": null_rate,
        "quotes": {"text_claims": text_claims, "unsupported": unsupported},
        "unsupported_quote_rate": unsupported / text_claims if text_claims else None,
        "seconds_per_panel": (
            {"p50": _percentile(seconds, 0.5), "p95": _percentile(seconds, 0.95)}
            if seconds
            else None
        ),
        "injection": {
            "pairs": pairs,
            "changes_per_field": per_field,
            "followed_instruction": followed,
        },
        "peak_rss_mb": peak_rss_mb,
    }


def fp32_exceeds_memory(bf16_weight_bytes: int, limit_bytes: int) -> bool:
    """fp32 holds twice the bytes of the bf16 files on disk; keep room for activations."""
    return bf16_weight_bytes * 2 > FP32_BUDGET * limit_bytes


# -- running a model -------------------------------------------------------------------------


def _spec_and_weights(model: str) -> tuple[ModelSpec, int]:
    from orchestration.models_fetch import load_models
    from pipeline.providers.registry import load_yaml

    for m in load_models(load_yaml("models.yaml")):
        if m.name == model and m.status == "approved":
            weights = sum(f.size_bytes for f in m.files if f.format == "safetensors")
            spec = ModelSpec("understand", "local", m.name, m.revision, m.license, m.source)
            return spec, weights
    raise LookupError(f"{model}: no approved model in models.yaml")


def process_panels(
    call: Callable[[BakePanel], PanelReading], panels: list[BakePanel]
) -> list[PanelResult]:
    out: list[PanelResult] = []
    for p in panels:
        start = time.perf_counter()
        try:
            reading: PanelReading | None = call(p)
        except StructuredOutputFailed:
            reading = None
        out.append(
            PanelResult(p.panel_id, p.case, reading, p.ocr_text, time.perf_counter() - start)
        )
    return out


def _make_call(
    model: str, dtype: str, settings: UnderstandSettings, instructions: str
) -> Callable[[BakePanel], PanelReading]:
    from pipeline.providers.local_vlm import LocalVlmProvider

    spec, _ = _spec_and_weights(model)
    provider = LocalVlmProvider(settings.model_copy(update={"dtype": dtype}))
    return lambda p: provider.vlm_structured(spec, instructions, p.ocr_text, [p.png], PanelReading)


def _child(model: str, dtype: str, panels: list[BakePanel], out_file: str) -> None:
    from pipeline.understand.settings import load_settings, prompt_text

    settings = load_settings()
    instructions, _ = prompt_text(settings)
    status = "ok"
    results: list[PanelResult] = []
    try:
        call = _make_call(model, dtype, settings, instructions)
        results = process_panels(call, panels)
    except Exception as e:  # model load failure etc.; the class name only, never a message
        status = f"failed: {type(e).__name__}"
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux
    Path(out_file).write_text(
        json.dumps(
            {
                "status": status,
                "peak_rss_mb": rss_mb,
                "results": [
                    {
                        "panel_id": r.panel_id,
                        "case": r.case,
                        "seconds": r.seconds,
                        "reading": r.reading.model_dump() if r.reading else None,
                    }
                    for r in results
                ],
            }
        ),
        encoding="utf-8",
    )


def run_one(model: str, dtype: str, panels: list[BakePanel], out: Path) -> RunResult:
    """Run one (model, dtype) in a fresh child process so its peak RSS is its own."""
    out.mkdir(parents=True, exist_ok=True)
    out_file = out / f"{model}-{dtype}.raw.json"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_child, args=(model, dtype, panels, str(out_file)))
    proc.start()
    proc.join()
    if proc.exitcode != 0 or not out_file.exists():
        return RunResult(model, dtype, f"failed: exit {proc.exitcode}")
    raw = json.loads(out_file.read_text(encoding="utf-8"))
    out_file.unlink()  # the panel text lives in memory; only the sheet and metrics are kept
    by_id = {p.panel_id: p for p in panels}
    results = [
        PanelResult(
            r["panel_id"],
            r["case"],
            PanelReading.model_validate(r["reading"]) if r["reading"] else None,
            by_id[r["panel_id"]].ocr_text,
            r["seconds"],
        )
        for r in raw["results"]
    ]
    return RunResult(model, dtype, raw["status"], results, raw["peak_rss_mb"])


# -- recording synthetic fixtures ------------------------------------------------------------


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def record_cases(
    panels: list[BakePanel],
    fixtures_dir: Path,
    instructions: str,
    call: Callable[[BakePanel], list[str]],
) -> list[Path]:
    """Write one replay fixture per synthetic panel. ``call`` returns the raw model text of
    each attempt. Chapter panels are refused before anything is written."""
    if not all(p.synthetic for p in panels):
        raise ValueError("only synthetic panels may be recorded as fixtures")
    paths: list[Path] = []
    for p in panels:
        if p.case is None:  # unreachable after the check above; keeps the type narrow
            raise ValueError("only synthetic panels may be recorded as fixtures")
        paths.append(
            record_case(
                fixtures_dir,
                p.case,
                _sha(instructions.encode("utf-8")),
                _sha(p.png),
                _sha(fence_untrusted(p.ocr_text).encode("utf-8")),
                call(p),
            )
        )
    return paths


def _recording_call(
    model: str, dtype: str, settings: UnderstandSettings, instructions: str
) -> Callable[[BakePanel], list[str]]:
    """Run the real provider and capture each attempt's raw text via its structured helper."""
    from pipeline.providers import local_vlm

    call = _make_call(model, dtype, settings, instructions)
    real = getattr(local_vlm, _TAP_TARGET)

    def capture(p: BakePanel) -> list[str]:
        seen: list[str] = []

        def wrapped(spec: ModelSpec, schema: Any, generate: Callable[[int], str], n: int) -> Any:
            def tap(attempt: int) -> str:
                text = generate(attempt)
                seen.append(text)
                return text

            return real(spec, schema, tap, n)

        setattr(local_vlm, _TAP_TARGET, wrapped)
        try:
            with contextlib.suppress(StructuredOutputFailed):  # still a faithful recording
                call(p)
        finally:
            setattr(local_vlm, _TAP_TARGET, real)
        return seen

    return capture


# -- contact sheet ---------------------------------------------------------------------------


def _fmt(reading: PanelReading | None) -> str:
    if reading is None:
        return "<i>no valid answer</i>"
    rows = []
    for n in CLAIM_FIELDS:
        c: Claim = getattr(reading, n)
        shown = "null" if _is_null(c) else f"{c.value} [{c.evidence}]"
        rows.append(f"<b>{n}</b>: {html.escape(shown)}")
    rows.append(f"<b>shot_hint</b>: {html.escape(str(reading.shot_hint))}")
    rows.append(f"<b>figures</b>: {len(reading.figures)}")
    return "<br>".join(rows)


def contact_sheet(panels: list[BakePanel], runs: list[RunResult]) -> str:
    ok = [r for r in runs if r.status == "ok"]
    head = "".join(f"<th>{html.escape(r.model)}<br>{html.escape(r.dtype)}</th>" for r in ok)
    body = []
    for p in panels:
        cells = []
        for r in ok:
            hit = next((x for x in r.results if x.panel_id == p.panel_id), None)
            cells.append(f"<td>{_fmt(hit.reading) if hit else ''}</td>")
        img = base64.b64encode(p.png).decode("ascii")
        label = html.escape(p.case or p.panel_id)
        body.append(
            f'<tr><td><div>{label}</div><img src="data:image/png;base64,{img}" width="220"></td>'
            + "".join(cells)
            + "</tr>"
        )
    return (
        '<!doctype html><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "img-src data:; style-src 'unsafe-inline'\">"
        "<title>Understand bake-off</title>"
        "<style>body{font:13px sans-serif}td,th{border:1px solid #999;padding:6px;"
        "vertical-align:top;max-width:300px}</style>"
        f"<table><tr><th>panel</th>{head}</tr>{''.join(body)}</table>"
    )


# -- chapter panels --------------------------------------------------------------------------


def load_chapter_panels(chapter_id: str, first: int, last: int, limit: int) -> list[BakePanel]:
    """Panels of pages ``first..last`` (reading position), crops from the store, text from the
    bubbles. Refuses unless the project's rights record allows rendering."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from orchestration.flows import db_rights_loader
    from pipeline.common.database import make_engine
    from pipeline.common.dbmodels import BubbleRow, ChapterRow, PageRow, PanelRow
    from pipeline.common.schemas import require_render_allowed
    from pipeline.common.settings import get_settings
    from pipeline.common.storage import make_store

    settings = get_settings()
    engine = make_engine(settings)
    store = make_store(settings)
    with Session(engine) as db:
        chapter = db.get(ChapterRow, chapter_id)
        if chapter is None:
            raise LookupError("no such chapter")
        require_render_allowed(db_rights_loader(engine)(chapter.project_id), datetime.now(UTC))
        rows = db.execute(
            select(PanelRow)
            .join(PageRow, PageRow.id == PanelRow.page_id)
            .where(PanelRow.chapter_id == chapter_id, PageRow.index.between(first, last))
            .order_by(PageRow.index, PanelRow.reading_order)
            .limit(limit)
        ).scalars()
        panels: list[BakePanel] = []
        for row in rows:
            texts = db.execute(
                select(BubbleRow.artifact)
                .where(BubbleRow.panel_id == row.id)
                .order_by(BubbleRow.reading_order)
            ).scalars()
            ocr = "\n".join(str(a.get("text", "")) for a in texts)
            panels.append(BakePanel(row.id, store.get(row.artifact["crop_ref"]), ocr))
    return panels


# -- CLI -------------------------------------------------------------------------------------


def _cgroup_limit_bytes() -> int:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 50:
            return int(raw)
    return 10 * GB


def _pages(text: str) -> tuple[int, int]:
    a, _, b = text.partition("-")
    first, last = int(a), int(b or a)
    if not 1 <= first <= last:
        raise argparse.ArgumentTypeError("pages must look like 7-20")
    return first, last


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="understand_bakeoff")
    ap.add_argument("--chapter")
    ap.add_argument("--pages", type=_pages, default=(1, 1000))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--models", default="qwen3-vl-2b-instruct,qwen3-5-2b,qwen3-5-0-8b", type=str)
    ap.add_argument("--dtypes", default="bfloat16,float32", type=str)
    ap.add_argument("--out", type=Path, default=Path("/reports/understand-bakeoff"))
    ap.add_argument("--record", type=Path, help="write replay fixtures from synthetic cases only")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    models = [m for m in args.models.split(",") if m]
    dtypes = [d for d in args.dtypes.split(",") if d]
    synthetic = [
        BakePanel(f"synthetic_{c.name}", c.png, c.ocr_text, c.name) for c in synthetic_cases()
    ]

    from pipeline.understand.settings import load_settings, prompt_text

    settings = load_settings()
    instructions, _ = prompt_text(settings)

    if args.record is not None:
        if len(models) != 1:
            ap.error("--record needs exactly one model")
        args.record.mkdir(parents=True, exist_ok=True)
        dtype = dtypes[0]
        paths = record_cases(
            synthetic,
            args.record,
            instructions,
            _recording_call(models[0], dtype, settings, instructions),
        )
        log.info("recorded %d synthetic fixtures", len(paths))
        return 0

    if not args.chapter:
        ap.error("--chapter is required")
    panels = [
        *load_chapter_panels(args.chapter, args.pages[0], args.pages[1], args.limit),
        *synthetic,
    ]
    log.info("bake-off on %d panels (%d synthetic)", len(panels), len(synthetic))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / stamp
    limit = _cgroup_limit_bytes()
    runs: list[RunResult] = []
    for model in models:
        _, weights = _spec_and_weights(model)
        for dtype in dtypes:
            if dtype == "float32" and fp32_exceeds_memory(weights, limit):
                log.info("%s %s: %s", model, dtype, SKIPPED_MEMORY)
                runs.append(RunResult(model, dtype, SKIPPED_MEMORY))
                continue
            log.info("%s %s: running", model, dtype)
            runs.append(run_one(model, dtype, panels, out))
            log.info("%s %s: %s", model, dtype, runs[-1].status)
    report = {
        f"{r.model}/{r.dtype}": {"status": r.status, **metrics(r.results, r.peak_rss_mb)}
        for r in runs
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "contact-sheet.html").write_text(contact_sheet(panels, runs), encoding="utf-8")
    log.info("report written to %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
