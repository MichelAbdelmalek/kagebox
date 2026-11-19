"""The ``chapter-ingest`` Prefect flow: source file -> Pages, Panels, Bubbles and review items.

Status goes ``pending`` -> ``ingesting`` -> ``ready`` | ``failed``. The rights gate runs first
and again just before the results are written, so a record revoked mid-run stores nothing.

Atomicity: pages are processed in memory (images go to the content-addressed store, which is
idempotent); the chapter's previous pages, panels, bubbles and ingest review items are replaced
and the status set to ``ready`` in ONE final transaction that locks the chapter row. A failure
before it leaves the previous complete set (or nothing) and status ``failed``, never a ``ready``
chapter with partial data. ``chapter.error`` holds only a user-safe message: the
``UploadRejected`` / ``RightsDenied`` message, or ``"ingest failed"``; details go to the log.

Sources: ``upload`` reads ``source_ref`` (a ``media`` object reference) from the object store
into a private temp directory; ``import`` is a bare file name in the read-only imports dir,
re-validated here (no separators, not ``.``/``..``, a regular file, not a symlink). Either way
the bytes are re-hashed against ``source_sha256`` before use. A single image becomes a
one-page image folder. ``manhwa_vertical`` projects read pages at full height and cut each with
``slice_strip``; every slice is its own Page, numbered 1.. across the chapter.

Models load once per run (one detector, one OCR for the chapter's language).
"""

# No `from __future__ import annotations`: Prefect builds a Pydantic model from the flow
# signature at run time and needs real (not string) annotations.

import contextlib
import hashlib
import importlib.metadata
import logging
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import filetype
import numpy as np
from prefect import flow
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from orchestration.flows import StageResult, db_rights_loader
from orchestration.models_fetch import load_models
from pipeline.common.dbmodels import (
    BubbleRow,
    ChapterRow,
    PageRow,
    PanelRow,
    ProjectRow,
    ReviewItemRow,
)
from pipeline.common.ids import new_id
from pipeline.common.schemas import RightsDenied, require_render_allowed
from pipeline.common.storage import ObjectStore, parse_ref
from pipeline.common.uploads import IMAGE_MIMES, UploadRejected
from pipeline.ingest import bubbles
from pipeline.ingest.panels import Direction, slice_strip
from pipeline.ingest.process import (
    REVIEW_KINDS,
    Detector,
    ModelInfo,
    Ocr,
    PageContext,
    PageResult,
    process_page,
)
from pipeline.ingest.sources import MAX_IMAGE_BYTES, Fit, SourceKind, detect_kind, iter_pages
from pipeline.providers.registry import load_yaml
from pipeline.providers.weights import load_conversions

log = logging.getLogger(__name__)

FLOW_NAME = "chapter-ingest"
DETECT_THRESHOLD = 0.5
KAGEBOX_TOOL = "kagebox-ingest"
KAGEBOX_LICENSE = "LicenseRef-Proprietary"  # pyproject.toml
DIRECTIONS: dict[str, Direction] = {
    "manga_rtl": "rtl",
    "comic_ltr": "ltr",
    "manhwa_vertical": "ltr",
}

GENERIC_ERROR = "ingest failed"
IMPORT_MISSING = "the imported source file is not available"
UPLOAD_MISSING = "the uploaded source file is not available"
SOURCE_CHANGED = "the source file changed since it was added"
MANHWA_PDF = "manhwa sources must be images or CBZ"
_CHUNK = 1024 * 1024


class IngestSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pages: int
    panels: int
    bubbles: int
    flagged: int  # review items created by this run


@dataclass(frozen=True)
class IngestDeps:
    engine: Engine
    store: ObjectStore
    imports_dir: Path
    detector_factory: Callable[[], Detector]
    ocr_factory: Callable[[str], Ocr]


@dataclass(frozen=True)
class _Chapter:
    id: str
    project_id: str
    format: str  # the project's format
    language: str
    source_kind: str
    source_ref: str
    source_sha256: str
    page_start: int
    page_end: int


def build_deps() -> IngestDeps:
    """Production wiring (tests monkeypatch this)."""
    from pipeline.common.database import make_engine  # deferred: needs settings at run time
    from pipeline.common.settings import get_settings
    from pipeline.common.storage import make_store
    from pipeline.ingest.ocr import ocr_for

    settings = get_settings()
    return IngestDeps(
        engine=make_engine(settings),
        store=make_store(settings),
        imports_dir=Path(settings.imports_dir),
        detector_factory=bubbles.BubbleDetector,
        ocr_factory=ocr_for,
    )


def load_thresholds() -> dict[str, Any]:
    return load_yaml("thresholds.yaml")


def model_info(name: str) -> ModelInfo:
    """Revision, licence and source of an approved model or conversion in models.yaml."""
    raw = load_yaml("models.yaml")
    entries = [(m.name, m.status, m.revision, m.license, m.source) for m in load_models(raw)]
    entries += [(c.name, c.status, c.revision, c.license, c.source) for c in load_conversions(raw)]
    for entry_name, status, revision, licence, source in entries:
        if entry_name == name and status == "approved":
            return ModelInfo(name, revision, licence, source)
    raise LookupError(f"{name}: no approved model in models.yaml")


def kagebox_info() -> ModelInfo:
    try:
        version = importlib.metadata.version("kagebox")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return ModelInfo(KAGEBOX_TOOL, version, KAGEBOX_LICENSE, None, is_model=False)


# ---------------------------------------------------------------------------------------------
# source file


def _import_path(root: Path, name: str) -> Path:
    bad = not name or name in (".", "..") or any(c in name for c in "/\\\x00")
    if bad:
        raise UploadRejected(IMPORT_MISSING)
    path = root / name
    try:
        st = os.lstat(path)
    except OSError:
        raise UploadRejected(IMPORT_MISSING) from None
    if not stat.S_ISREG(st.st_mode):  # lstat: a symlink is not a regular file
        raise UploadRejected(IMPORT_MISSING)
    return path


def _hash_import(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise UploadRejected(IMPORT_MISSING) from None
    h = hashlib.sha256()
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise UploadRejected(IMPORT_MISSING)
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _image_ext(path: Path) -> str | None:
    with path.open("rb") as f:
        kind = filetype.guess(f.read(8192))
    return IMAGE_MIMES.get(kind.mime) if kind is not None else None


@contextlib.contextmanager
def open_source(ch: _Chapter, deps: IngestDeps) -> Iterator[tuple[Path, SourceKind]]:
    """The verified source as (path, kind); temp files are removed on exit."""
    with tempfile.TemporaryDirectory(prefix="kagebox-ingest-") as tmp:
        if ch.source_kind == "import":
            path = _import_path(deps.imports_dir, ch.source_ref)
            if _hash_import(path) != ch.source_sha256:
                raise UploadRejected(SOURCE_CHANGED)
        elif ch.source_kind == "upload":
            try:
                bucket, _ = parse_ref(ch.source_ref)
                if bucket != "media":
                    raise ValueError("not a media reference")
                data = deps.store.get(ch.source_ref)
            except (KeyError, ValueError):
                raise UploadRejected(UPLOAD_MISSING) from None
            if hashlib.sha256(data).hexdigest() != ch.source_sha256:
                raise UploadRejected(SOURCE_CHANGED)
            path = Path(tmp) / "source"
            path.write_bytes(data)
            del data
        else:
            raise ValueError(f"unknown source kind {ch.source_kind!r}")

        ext = _image_ext(path)
        if ext is None:
            yield path, detect_kind(path)
            return
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise UploadRejected("image is too large")
        folder = Path(tmp) / "pages"
        folder.mkdir()
        (folder / f"1.{ext}").write_bytes(path.read_bytes())
        yield folder, "images"


# ---------------------------------------------------------------------------------------------
# database


def _start(engine: Engine, chapter_id: str) -> _Chapter:
    with Session(engine) as s, s.begin():
        row = s.get(ChapterRow, chapter_id, with_for_update=True)
        if row is None:
            raise LookupError("chapter not found")
        project = s.get(ProjectRow, row.project_id)
        if project is None:
            raise LookupError("project not found")
        row.status, row.error = "ingesting", None
        return _Chapter(
            id=row.id,
            project_id=row.project_id,
            format=project.format,
            language=row.language,
            source_kind=row.source_kind,
            source_ref=row.source_ref,
            source_sha256=row.source_sha256,
            page_start=row.page_start,
            page_end=row.page_end,
        )


def _fail(engine: Engine, chapter_id: str, message: str) -> None:
    with Session(engine) as s, s.begin():
        row = s.get(ChapterRow, chapter_id)
        if row is not None:
            row.status, row.error = "failed", message


def _commit(engine: Engine, ch: _Chapter, results: list[PageResult]) -> int:
    """Replace the chapter's ingest output and mark it ready, in one transaction."""
    with Session(engine) as s, s.begin():
        row = s.get(ChapterRow, ch.id, with_for_update=True)
        if row is None:
            raise LookupError("chapter not found")
        s.execute(
            delete(ReviewItemRow).where(
                ReviewItemRow.chapter_id == ch.id, ReviewItemRow.kind.in_(REVIEW_KINDS)
            )
        )
        for model in (BubbleRow, PanelRow, PageRow):
            s.execute(delete(model).where(model.chapter_id == ch.id))
        for r in results:
            s.add(
                PageRow(
                    id=r.page.id,
                    chapter_id=ch.id,
                    index=r.page.index,
                    artifact=r.page.model_dump(mode="json"),
                )
            )
        s.flush()
        for r in results:
            for p in r.panels:
                s.add(
                    PanelRow(
                        id=p.id,
                        page_id=p.page_id,
                        chapter_id=ch.id,
                        reading_order=p.reading_order,
                        artifact=p.model_dump(mode="json"),
                    )
                )
        s.flush()
        flagged = 0
        for r in results:
            for b in r.bubbles:
                s.add(
                    BubbleRow(
                        id=b.id,
                        panel_id=b.panel_id,
                        chapter_id=ch.id,
                        needs_review=b.needs_review,
                        artifact=b.model_dump(mode="json", exclude={"needs_review"}),
                    )
                )
            for note in r.reviews:
                flagged += 1
                s.add(
                    ReviewItemRow(
                        id=new_id("rev"),
                        project_id=ch.project_id,
                        chapter_id=ch.id,
                        artifact_id=note.artifact_id,
                        kind=note.kind,
                        reason=note.reason,
                        status="open",
                    )
                )
        row.status, row.error = "ready", None
        return flagged


# ---------------------------------------------------------------------------------------------
# the run


def _check_rights(engine: Engine, project_id: str) -> None:
    require_render_allowed(db_rights_loader(engine)(project_id), datetime.now(UTC))


def _ingest(ch: _Chapter, deps: IngestDeps) -> IngestSummary:
    _check_rights(deps.engine, ch.project_id)
    direction = DIRECTIONS[ch.format]
    manhwa = ch.format == "manhwa_vertical"
    irregular = float(load_thresholds()["panel_irregular_fill"])
    results: list[PageResult] = []
    with open_source(ch, deps) as (path, kind):
        if manhwa and kind == "pdf":
            raise UploadRejected(MANHWA_PDF)
        detector = deps.detector_factory()
        ocr = deps.ocr_factory(ch.language)
        ctx = PageContext(
            chapter_id=ch.id,
            language=ch.language,
            direction=direction,
            source_sha256=ch.source_sha256,
            store=deps.store,
            detector=detector,
            ocr=ocr,
            kagebox=kagebox_info(),
            detector_info=model_info(bubbles.MODEL_NAME),
            ocr_info=model_info(ocr.engine),
            detect_threshold=DETECT_THRESHOLD,
            irregular_fill=irregular,
            now=datetime.now(UTC),
        )
        fit: Fit = "width" if manhwa else "long_side"
        for number, img in iter_pages(path, kind, ch.page_start, ch.page_end, fit=fit):
            if not manhwa:
                results.append(process_page(ctx, number, img))
                continue
            for y0, y1 in slice_strip(np.asarray(img)):
                piece = img.crop((0, y0, img.width, y1))
                results.append(process_page(ctx, len(results) + 1, piece))
            log.info("page %d done (%d pages so far)", number, len(results))
    _check_rights(deps.engine, ch.project_id)  # revoked mid-run: store nothing
    flagged = _commit(deps.engine, ch, results)
    return IngestSummary(
        pages=len(results),
        panels=sum(len(r.panels) for r in results),
        bubbles=sum(len(r.bubbles) for r in results),
        flagged=flagged,
    )


def run_ingest(chapter_id: str, deps: IngestDeps) -> IngestSummary:
    ch = _start(deps.engine, chapter_id)
    try:
        summary = _ingest(ch, deps)
    except (UploadRejected, RightsDenied) as exc:
        log.warning("chapter %s refused: %s", chapter_id, exc)
        _fail(deps.engine, chapter_id, str(exc))
        raise
    except Exception:
        log.exception("chapter %s ingest failed", chapter_id)
        _fail(deps.engine, chapter_id, GENERIC_ERROR)
        raise
    log.info("chapter %s ready: %s", chapter_id, summary)
    return summary


@flow(name=FLOW_NAME, log_prints=False, validate_parameters=True)
def ingest_flow(chapter_id: str) -> IngestSummary:
    """Deployment entry point. Parameters must stay JSON-serializable."""
    return run_ingest(chapter_id, build_deps())


def ingest_stage(chapter_id: str) -> StageResult:
    """The chapter pipeline's ingest stage."""
    summary = ingest_flow(chapter_id)
    counts = {k: str(v) for k, v in summary.model_dump().items()}
    return {"stage": "ingest", "chapter_id": chapter_id, "status": "ready", **counts}
