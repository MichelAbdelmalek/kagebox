"""The ``chapter-ingest`` Prefect flow: source file -> Pages, Panels, Bubbles and review items.

Status goes ``pending`` -> ``ingesting`` -> ``ready`` | ``failed``. Taking the chapter is one
conditional UPDATE: it succeeds only if no other run holds it (``status <> 'ingesting'``) or
that run's last heartbeat (``ingest_heartbeat_at``, else its start) is more than
``STALE_INGEST_S`` old; otherwise ``IngestAlreadyRunning`` is raised and the chapter is not
touched. The owning run beats at start, after loading models and after every page; a beat that
finds the chapter no longer owned stops the run (``IngestAlreadyRunning``).
``chapters.ingest_run_id`` (the Prefect flow-run id, or a fresh token for a direct call)
identifies the owning run: a run that was taken over writes nothing and leaves the status alone,
and the cancellation / crash hook only fails the chapter when the interrupted flow run is its
owner.

The rights gate runs first, and again inside the final transaction after the chapter row is
locked, so a record revoked mid-run stores no rows.

Atomicity: pages are processed in memory; page and crop images go to the content-addressed
store as they are made. The chapter's previous pages, panels, bubbles and ingest review items
are replaced and the status set to ``ready`` in ONE final transaction that locks the chapter
row. A failure before it leaves the previous complete set (or nothing) and status ``failed``,
never a ``ready`` chapter with partial data. Objects stored by a refused or failed run may stay
in the store (unreferenced; rows never reference a missing object).

``chapter.error`` holds only a user-safe message: the ``UploadRejected`` / ``RightsDenied``
message, ``"ingest failed"``, ``"ingest was interrupted"`` (cancellation, termination or a
crash: a ``BaseException`` in the run, or Prefect's ``on_cancellation`` / ``on_crashed`` hooks),
or ``"ingest could not start"`` (the run ended before taking the chapter: cancelled while queued,
``build_deps`` failed, crashed early; set by the hooks, including ``on_failure``, only while the
chapter is still ``pending`` and its latest job is this run). Details go to the log; validation
errors are logged by field and type only, never with input.

Jobs: the API records a ``jobs`` row per started run (``flow_run_id``); the run moves it to
``running`` once it takes the chapter, then ``completed`` / ``failed``, and the hooks set
``cancelled`` / ``failed`` for runs that end otherwise. A finished job keeps its status.

Sources: ``upload`` reads ``source_ref`` (a ``media`` object reference) from the object store
into a private temp directory; ``import`` is a bare file name in the project's own directory of
the read-only imports dir (``<imports>/<project_id>/``), re-validated here (no separators, not
``.``/``..``, a real directory and a regular file, no symlinks). Either way the bytes are
re-hashed against ``source_sha256`` before use. A single image becomes a one-page image folder.

``Page.index`` is the 1-based reading position in the chapter; ``Page.source_page`` the page
number in the source file. ``manhwa_vertical`` projects read pages at full height and cut each
with ``slice_strip``; every slice is its own Page with its ``source_y0``/``source_y1`` rows.

Models load once per run (one detector, one OCR for the chapter's language).
"""

# No `from __future__ import annotations`: Prefect builds a Pydantic model from the flow
# signature at run time and needs real (not string) annotations.

import contextlib
import hashlib
import importlib.metadata
import json
import logging
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import filetype
import numpy as np
from prefect import Flow, flow
from prefect.client.schemas.objects import FlowRun, State
from prefect.runtime import flow_run as prefect_flow_run
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import CursorResult, Engine, delete, select, update
from sqlalchemy.orm import Session

from orchestration.flows import StageResult, db_rights_loader
from orchestration.models_fetch import load_models
from pipeline.common.dbmodels import (
    BubbleRow,
    ChapterRow,
    JobRow,
    PageRow,
    PanelRow,
    ProjectRow,
    ReviewItemRow,
    RightsRow,
    ingest_stale_before,
    ingest_takeover_allowed,
)
from pipeline.common.ids import new_id
from pipeline.common.schemas import RightsDenied, RightsRecord, require_render_allowed
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
    SourcePosition,
    process_page,
    safe_errors,
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
INTERRUPTED = "ingest was interrupted"
NOT_STARTED = "ingest could not start"
JOB_OPEN = ("submitted", "running")  # job statuses a run may still move on
ALREADY_RUNNING = "ingest already running"
IMPORT_MISSING = "the imported source file is not available"
UPLOAD_MISSING = "the uploaded source file is not available"
SOURCE_CHANGED = "the source file changed since it was added"
MANHWA_PDF = "manhwa sources must be images or CBZ"
_CHUNK = 1024 * 1024
_PROJECT_ID = re.compile(r"^proj_[0-9a-z_]{1,64}$")  # pipeline.common.ids.ProjectId


class IngestAlreadyRunning(RuntimeError):
    """Another run holds the chapter. The message is safe to show to the user."""


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
    run_id: str  # this run's owner token


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


def _import_path(root: Path, project_id: str, name: str) -> Path:
    """``<root>/<project_id>/<name>``: the project's own imports directory only.

    The project id comes from the database and is re-checked against the id pattern; the
    directory must be a real directory and the file a regular file (``lstat``: symlinks are
    neither followed nor accepted at either level).
    """
    bad = not name or name in (".", "..") or any(c in name for c in "/\\\x00")
    if bad or not _PROJECT_ID.match(project_id):
        raise UploadRejected(IMPORT_MISSING)
    folder = root / project_id
    path = folder / name
    try:
        folder_st, st = os.lstat(folder), os.lstat(path)
    except OSError:
        raise UploadRejected(IMPORT_MISSING) from None
    if not stat.S_ISDIR(folder_st.st_mode) or not stat.S_ISREG(st.st_mode):
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
            path = _import_path(deps.imports_dir, ch.project_id, ch.source_ref)
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


def current_run_id() -> str:
    """The current Prefect flow-run id; a fresh token outside a flow run."""
    run_id = prefect_flow_run.id
    return str(run_id) if run_id else f"direct-{uuid.uuid4()}"


def _start(engine: Engine, chapter_id: str, run_id: str) -> _Chapter:
    """Take the chapter for this run with one conditional UPDATE, or refuse."""
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        res = cast(
            "CursorResult[Any]",
            s.execute(
                update(ChapterRow)
                .where(
                    ChapterRow.id == chapter_id, ingest_takeover_allowed(ingest_stale_before(now))
                )
                .values(
                    status="ingesting",
                    error=None,
                    ingest_started_at=now,
                    ingest_heartbeat_at=now,
                    ingest_run_id=run_id,
                )
                .execution_options(synchronize_session=False)
            ),
        )
        row = s.get(ChapterRow, chapter_id)
        if row is None:
            raise LookupError("chapter not found")
        if res.rowcount != 1:
            raise IngestAlreadyRunning(ALREADY_RUNNING)
        project = s.get(ProjectRow, row.project_id)
        if project is None:
            raise LookupError("project not found")
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
            run_id=run_id,
        )


def _beat(engine: Engine, ch: _Chapter) -> None:
    """Heartbeat while this run owns the chapter; a run that lost it stops here."""
    with Session(engine) as s, s.begin():
        res = cast(
            "CursorResult[Any]",
            s.execute(
                update(ChapterRow)
                .where(
                    ChapterRow.id == ch.id,
                    ChapterRow.status == "ingesting",
                    ChapterRow.ingest_run_id == ch.run_id,
                )
                .values(ingest_heartbeat_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            ),
        )
        if res.rowcount != 1:
            raise IngestAlreadyRunning(ALREADY_RUNNING)


def _owns(row: ChapterRow, run_id: str) -> bool:
    return row.status == "ingesting" and row.ingest_run_id == run_id


def _fail(engine: Engine, chapter_id: str, message: str, run_id: str) -> None:
    """Mark the chapter failed, only while ``run_id`` still owns it."""
    with Session(engine) as s, s.begin():
        row = s.get(ChapterRow, chapter_id, with_for_update=True)
        if row is not None and _owns(row, run_id):
            row.status, row.error = "failed", message


def _safe_fail(engine: Engine, chapter_id: str, message: str, run_id: str) -> None:
    """``_fail`` that never raises, so the caller re-raises the ORIGINAL error."""
    try:
        _fail(engine, chapter_id, message, run_id)
    except Exception:
        log.exception("could not mark chapter %s failed", chapter_id)


def _rights_in(s: Session, project_id: str) -> None:
    raw = s.scalar(select(RightsRow.record).where(RightsRow.project_id == project_id))
    rec = None if raw is None else RightsRecord.model_validate_json(json.dumps(raw))
    require_render_allowed(rec, datetime.now(UTC))


def _commit(engine: Engine, ch: _Chapter, results: list[PageResult]) -> int:
    """Replace the chapter's ingest output and mark it ready, in one transaction."""
    with Session(engine) as s, s.begin():
        row = s.get(ChapterRow, ch.id, with_for_update=True)
        if row is None:
            raise LookupError("chapter not found")
        if not _owns(row, ch.run_id):
            raise IngestAlreadyRunning(ALREADY_RUNNING)  # taken over: the new owner writes
        _rights_in(s, ch.project_id)  # under the chapter lock, in the writing transaction
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
                        reading_order=b.reading_order,
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
    _check_rights(deps.engine, ch.project_id)  # fail fast; re-checked inside _commit
    direction = DIRECTIONS[ch.format]
    manhwa = ch.format == "manhwa_vertical"
    irregular = float(load_thresholds()["panel_irregular_fill"])
    results: list[PageResult] = []
    with open_source(ch, deps) as (path, kind):
        if manhwa and kind == "pdf":
            raise UploadRejected(MANHWA_PDF)
        detector = deps.detector_factory()
        ocr = deps.ocr_factory(ch.language)
        detector_info = model_info(bubbles.MODEL_NAME)
        fit: Fit = "width" if manhwa else "long_side"
        ctx = PageContext(
            chapter_id=ch.id,
            language=ch.language,
            direction=direction,
            fit=fit,
            source_sha256=ch.source_sha256,
            store=deps.store,
            detector=detector,
            ocr=ocr,
            kagebox=kagebox_info(),
            detector_info=detector_info,
            ocr_info=model_info(ocr.engine),
            components=(detector_info, *(model_info(m) for m in ocr.models)),
            detect_threshold=DETECT_THRESHOLD,
            irregular_fill=irregular,
            now=datetime.now(UTC),
        )
        _beat(deps.engine, ch)  # models loaded
        for number, img in iter_pages(path, kind, ch.page_start, ch.page_end, fit=fit):
            if not manhwa:
                position = SourcePosition(number)
                results.append(process_page(ctx, len(results) + 1, position, img))
                _beat(deps.engine, ch)
                continue
            for y0, y1 in slice_strip(np.asarray(img)):
                piece = img.crop((0, y0, img.width, y1))
                position = SourcePosition(number, y0, y1)
                results.append(process_page(ctx, len(results) + 1, position, piece))
                _beat(deps.engine, ch)
            log.info("page %d done (%d pages so far)", number, len(results))
    flagged = _commit(deps.engine, ch, results)
    return IngestSummary(
        pages=len(results),
        panels=sum(len(r.panels) for r in results),
        bubbles=sum(len(r.bubbles) for r in results),
        flagged=flagged,
    )


def run_ingest(chapter_id: str, deps: IngestDeps, run_id: str | None = None) -> IngestSummary:
    ch = _start(deps.engine, chapter_id, run_id or current_run_id())  # refused: nothing touched
    _safe_set_job(deps.engine, ch.run_id, "running")
    try:
        summary = _ingest(ch, deps)
    except IngestAlreadyRunning:
        log.warning("chapter %s was taken over by another run; nothing written", chapter_id)
        _safe_set_job(deps.engine, ch.run_id, "failed")
        raise
    except (UploadRejected, RightsDenied) as exc:
        log.warning("chapter %s refused: %s", chapter_id, exc)
        _safe_fail(deps.engine, chapter_id, str(exc), ch.run_id)
        _safe_set_job(deps.engine, ch.run_id, "failed")
        raise
    except ValidationError as exc:  # never log or re-raise it with its (untrusted) input
        errors = safe_errors(exc)
        log.error("chapter %s ingest failed: invalid artifact: %s", chapter_id, errors)
        _safe_fail(deps.engine, chapter_id, GENERIC_ERROR, ch.run_id)
        _safe_set_job(deps.engine, ch.run_id, "failed")
        raise RuntimeError(f"invalid artifact: {errors}") from None
    except Exception:
        log.exception("chapter %s ingest failed", chapter_id)
        _safe_fail(deps.engine, chapter_id, GENERIC_ERROR, ch.run_id)
        _safe_set_job(deps.engine, ch.run_id, "failed")
        raise
    except BaseException:  # cancellation, termination signal, KeyboardInterrupt
        # The job's final status (cancelled / failed) is left to the Prefect hooks.
        log.warning("chapter %s ingest interrupted", chapter_id)
        _safe_fail(deps.engine, chapter_id, INTERRUPTED, ch.run_id)
        raise
    _safe_set_job(deps.engine, ch.run_id, "completed")
    log.info("chapter %s ready: %s", chapter_id, summary)
    return summary


# ---------------------------------------------------------------------------------------------
# jobs and Prefect state hooks


def build_engine() -> Engine:
    """Database only, for the state hooks (tests monkeypatch this). Unlike ``build_deps`` it
    needs no object-store credentials, so a run whose ``build_deps`` failed is still recorded."""
    from pipeline.common.database import make_engine  # deferred: needs settings at run time
    from pipeline.common.settings import get_settings

    return make_engine(get_settings())


def _set_job(engine: Engine, run_id: str, status: str) -> None:
    """Move the API's job for this flow run on; a finished job keeps its final status."""
    with Session(engine) as s, s.begin():
        s.execute(
            update(JobRow)
            .where(JobRow.flow_run_id == run_id, JobRow.status.in_(JOB_OPEN))
            .values(status=status, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )


def _safe_set_job(engine: Engine, run_id: str, status: str) -> None:
    try:
        _set_job(engine, run_id, status)
    except Exception:
        log.exception("could not mark the job of run %s %s", run_id, status)


def _fail_unstarted(engine: Engine, chapter_id: str, run_id: str) -> None:
    """A run that ended before taking the chapter fails it, if the chapter still waits for
    exactly this run ('pending', and its latest job is this run): never 'pending' forever."""
    with Session(engine) as s, s.begin():
        row = s.get(ChapterRow, chapter_id, with_for_update=True)
        if row is None or row.status != "pending":
            return
        latest = s.scalar(
            select(JobRow.flow_run_id)
            .where(JobRow.chapter_id == chapter_id)
            .order_by(JobRow.created_at.desc(), JobRow.id.desc())
            .limit(1)
        )
        if latest == run_id:
            row.status, row.error = "failed", NOT_STARTED


def _after_run(flow_run: FlowRun, job_status: str, message: str) -> None:
    """Record how a run ended: its job, and its chapter if the run left it unfinished."""
    chapter_id = flow_run.parameters.get("chapter_id")
    run_id = str(flow_run.id)
    try:
        engine = build_engine()
    except Exception:
        log.exception("could not record the end of run %s", run_id)
        return
    steps: list[tuple[str, Callable[[], None]]] = [
        ("job", lambda: _set_job(engine, run_id, job_status)),
    ]
    if isinstance(chapter_id, str):
        steps += [
            ("owned chapter", lambda: _fail(engine, chapter_id, message, run_id)),
            ("pending chapter", lambda: _fail_unstarted(engine, chapter_id, run_id)),
        ]
    try:
        for what, step in steps:
            try:
                step()
            except Exception:
                log.exception("could not update the %s after run %s ended", what, run_id)
    finally:
        engine.dispose()


def on_cancelled(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any] | None) -> None:
    """Cancelled (also while still queued for the concurrency slot)."""
    _after_run(flow_run, "cancelled", INTERRUPTED)


def on_crashed(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any] | None) -> None:
    _after_run(flow_run, "failed", INTERRUPTED)


def on_failed(flow: Flow[Any, Any], flow_run: FlowRun, state: State[Any] | None) -> None:
    """Any failure, including one before the chapter was taken (``build_deps`` raising).
    ``run_ingest`` has usually recorded the precise message already; this only fills gaps."""
    _after_run(flow_run, "failed", GENERIC_ERROR)


@flow(
    name=FLOW_NAME,
    log_prints=False,
    validate_parameters=True,
    on_cancellation=[on_cancelled],
    on_crashed=[on_crashed],
    on_failure=[on_failed],
)
def ingest_flow(chapter_id: str) -> IngestSummary:
    """Deployment entry point. Parameters must stay JSON-serializable."""
    return run_ingest(chapter_id, build_deps())


def ingest_stage(chapter_id: str) -> StageResult:
    """The chapter pipeline's ingest stage."""
    summary = ingest_flow(chapter_id)
    counts = {k: str(v) for k, v in summary.model_dump().items()}
    return {"stage": "ingest", "chapter_id": chapter_id, "status": "ready", **counts}
