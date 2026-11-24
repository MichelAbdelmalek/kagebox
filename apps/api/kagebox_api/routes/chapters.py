"""Chapters: create, add a source (upload or server-side import), start ingest, read results.

Everything a user sends is untrusted: uploads are capped while streaming and sniffed by content;
import names are only ever compared against a listing of the project's own imports directory
(``<imports>/<project_id>/``), never joined into a path. Responses carry signed, user-bound
media URLs, never object keys or file paths. OCR text is returned as JSON data only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import IO, Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.types import Message

from kagebox_api.db import (
    BubbleRow,
    ChapterRow,
    JobRow,
    PageRow,
    PanelRow,
    ProjectRow,
    ReviewItemRow,
    RightsRow,
    User,
    audit,
    aware,
    utcnow,
)
from kagebox_api.deps import DB, AppState, CurrentUser, require_project_role, state
from kagebox_api.prefect_client import (
    UNDERSTAND_DEPLOYMENT,
    PrefectError,
    start_deployment,
    start_ingest,
)
from kagebox_api.security import sign_media
from pipeline.common.dbmodels import (
    SceneRow,
    StageRunRow,
    full_run_allowed,
    ingest_alive,
    ingest_stale_before,
    ingest_takeover_allowed,
    stage_alive,
    stage_takeover_allowed,
)
from pipeline.common.ids import new_id
from pipeline.common.schemas import (
    BBox,
    Bubble,
    Page,
    Panel,
    RightsDenied,
    RightsRecord,
    SceneJSON,
    require_render_allowed,
)
from pipeline.common.uploads import ARCHIVE_MIMES, DOC_MIMES, Sniffed, UploadRejected, sniff
from pipeline.ingest.sources import MAX_PAGES

log = logging.getLogger(__name__)

router = APIRouter(tags=["chapters"])

Language = Literal["ja", "en", "ko"]
NO_SOURCE = "none"  # source_kind of a chapter that has no source yet (columns are NOT NULL)
SOURCE_MIMES = {**DOC_MIMES, **ARCHIVE_MIMES}  # pdf, png, jpeg, webp, zip (stored as .cbz)
SNIFF_BYTES = 8192
CHUNK = 1024 * 1024
FORM_SLACK = 64 * 1024  # multipart boundaries, part headers and the two small range fields
MANHWA_PDF = "manhwa sources must be images or CBZ"  # same message as the ingest flow
START_FAILED = "could not start ingest; try again"
UNDERSTAND_START_FAILED = "could not start understanding; try again"
NOT_READY = "chapter is not ready"
UNDERSTAND_RUNNING = "understanding is already running"
BEING_UNDERSTOOD = "chapter is being understood"
TRIAL_REQUIRED = "run a trial first and confirm the full run"
MAX_TRIAL_PANELS = 50  # same bound as orchestration.understand_flow (the API must not import it)
UNDERSTAND = "understand"
# The only stage counts a client may see (numbers and booleans, never keys or diagnostics).
# The trial's projected total is published as ``project_total_s``.
SAFE_COUNTS = {
    "trial": "trial",
    "panels": "panels",
    "scenes": "scenes",
    "cached": "cached",
    "failed": "failed",
    "flagged": "flagged",
    "seconds_per_panel": "seconds_per_panel",
    "projected_total_s": "project_total_s",
}
INTERRUPTED = "ingest was interrupted"  # same message as the ingest flow
MAX_CONCURRENT_UPLOADS = 2
UPLOADS_BUSY = "too many uploads in progress, try again"
SPOOL_FAILED = "the server could not store the upload; try again later"
_DIGITS = re.compile(r"^[0-9]{1,6}$")
PROJECT_ID = re.compile(r"^proj_[0-9a-z_]{1,64}$")  # pipeline.common.ids.ProjectId


# ---------------------------------------------------------------------------------------------
# schemas


class ChapterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int = Field(ge=1, le=100_000, strict=True)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    language: Language


class PageRange(BaseModel):
    """1-based, inclusive. Bounds against the actual document are checked by the flow."""

    model_config = ConfigDict(extra="forbid", strict=True)
    page_start: int = Field(ge=1, le=MAX_PAGES)
    page_end: int = Field(ge=1, le=MAX_PAGES)

    @model_validator(mode="after")
    def _ordered(self) -> PageRange:
        if self.page_end < self.page_start:
            raise ValueError("page_end must not be before page_start")
        return self


class ImportIn(PageRange):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def _bare_name(cls, v: str) -> str:
        if any(c in v for c in "/\\\x00") or ".." in v or v == ".":
            raise ValueError("name must be a bare file name")
        return v


class ChapterOut(BaseModel):
    id: str
    project_id: str
    number: int
    title: str | None
    language: str
    format: str
    status: str
    error: str | None
    source_kind: Literal["upload", "import"] | None
    page_start: int | None
    page_end: int | None
    created_at: datetime
    ingest_started_at: datetime | None


class JobAccepted(BaseModel):
    job_id: str


class PageOut(BaseModel):
    id: str
    index: int
    source_page: int
    source_y0: int | None
    source_y1: int | None
    width: int
    height: int
    image_url: str


class BubbleOut(BaseModel):
    id: str
    reading_order: int
    bbox: BBox
    kind: str
    text: str  # untrusted OCR output: data only
    lang: str
    ocr_confidence: float
    ocr_engine: str
    needs_review: bool


class CitedOut(BaseModel):
    value: str | None  # untrusted model output: data only (length-bounded by the schema)
    panel_ids: list[str]


class FigureOut(BaseModel):
    description: CitedOut
    emotion: CitedOut


class SceneOut(BaseModel):
    setting: CitedOut
    action: CitedOut
    shot_hint: CitedOut
    time_of_day: CitedOut
    weather: CitedOut
    figures: list[FigureOut]
    unknown_fields: list[str]


class UnderstandIn(BaseModel):
    """``limit_panels`` makes a trial. Without it this is a full run, which needs
    ``confirm_full`` and a finished, current trial."""

    model_config = ConfigDict(extra="forbid", strict=True)
    limit_panels: int | None = Field(default=None, ge=1, le=MAX_TRIAL_PANELS)
    confirm_full: bool = False

    @model_validator(mode="after")
    def _one_mode(self) -> UnderstandIn:
        if self.limit_panels is not None and self.confirm_full:
            raise ValueError("a trial cannot confirm a full run")
        return self


class StageOut(BaseModel):
    stage: str
    status: str
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    error: str | None
    counts: dict[str, bool | int | float]
    live: bool
    interrupted: bool  # 'running' with no recent sign of life


class PanelOut(BaseModel):
    id: str
    reading_order: int
    bbox: BBox
    polygon: list[tuple[int, int]] | None
    crop_url: str
    bubbles: list[BubbleOut]
    scene: SceneOut | None = None


class ReviewItemOut(BaseModel):
    id: str
    chapter_id: str
    artifact_id: str
    kind: str
    reason: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------------------------
# helpers


def _out(row: ChapterRow) -> ChapterOut:
    has_source = row.source_kind in ("upload", "import")
    status_, error = row.status, row.error
    if status_ == "ingesting" and not _busy(row):  # the run died without a word (no heartbeat)
        status_, error = "failed", INTERRUPTED
    return ChapterOut(
        id=row.id,
        project_id=row.project_id,
        number=row.number,
        title=row.title,
        language=row.language,
        format=row.format,
        status=status_,
        error=error,
        source_kind=cast(Literal["upload", "import"], row.source_kind) if has_source else None,
        page_start=row.page_start if has_source else None,
        page_end=row.page_end if has_source else None,
        created_at=row.created_at,
        ingest_started_at=row.ingest_started_at,
    )


def _chapter_for(db: Session, user: User, chapter_id: str, minimum: str) -> ChapterRow:
    """The chapter if the user has ``minimum`` on its project; 404 alike for absent or hidden."""
    row = db.get(ChapterRow, chapter_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chapter not found")
    try:
        require_project_role(db, user, row.project_id, minimum)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "chapter not found") from None
        raise
    return row


def _require_rights(db: Session, user: User, project_id: str) -> None:
    row = db.scalar(select(RightsRow).where(RightsRow.project_id == project_id))
    rec = None if row is None else RightsRecord.model_validate_json(json.dumps(row.record))
    try:
        require_render_allowed(rec, utcnow())
    except RightsDenied as exc:
        audit(db, user.id, "ingest.denied", project_id, reason=str(exc))
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


def _busy(row: ChapterRow) -> bool:
    """'ingesting' and owned by a live run (recent heartbeat; same rule as the ingest flow)."""
    return ingest_alive(row, ingest_stale_before())


def _understanding(db: Session, chapter_id: str) -> bool:
    """The understand stage is held by a live run (stale heartbeats do not count)."""
    st = db.get(StageRunRow, (chapter_id, UNDERSTAND))
    return st is not None and stage_alive(st, ingest_stale_before())


def _prepare(db: Session, user: User, chapter_id: str) -> ChapterRow:
    """Checks shared by upload and import, before any bytes are read.

    Ends the transaction before returning, so no pooled connection is held while a large body
    is streamed or hashed (the session does not expire loaded objects on commit). The final
    conditional UPDATE re-checks the state.
    """
    row = _chapter_for(db, user, chapter_id, "editor")
    _require_rights(db, user, row.project_id)
    if _busy(row):
        raise HTTPException(status.HTTP_409_CONFLICT, "chapter is being ingested")
    if _understanding(db, row.id):
        raise HTTPException(status.HTTP_409_CONFLICT, BEING_UNDERSTOOD)
    db.commit()  # read-only so far; commit (not rollback, which expires) frees the connection
    return row


def _check_kind(row: ChapterRow, kind: Sniffed) -> None:
    if row.format == "manhwa_vertical" and kind.ext == "pdf":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MANHWA_PDF)


def _set_source_and_start(
    db: Session,
    st: AppState,
    user: User,
    row: ChapterRow,
    *,
    kind: Literal["upload", "import"],
    ref: str,
    sha256_hex: str,
    rng: PageRange,
) -> JobAccepted:
    """Record the verified source, then ask Prefect for a run. Refused while ingesting."""
    if _understanding(db, row.id):  # re-checked: a run may have started while the body streamed
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, BEING_UNDERSTOOD)
    result = cast(
        CursorResult[Any],
        db.execute(
            update(ChapterRow)
            .where(ChapterRow.id == row.id, ingest_takeover_allowed(ingest_stale_before()))
            .values(
                source_kind=kind,
                source_ref=ref,
                source_sha256=sha256_hex,
                page_start=rng.page_start,
                page_end=rng.page_end,
                status="pending",
                error=None,
                # Taking over a stale run: it no longer owns the chapter and writes nothing.
                ingest_started_at=None,
                ingest_heartbeat_at=None,
                ingest_run_id=None,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "chapter is being ingested")
    audit(db, user.id, f"chapter.{kind}", row.id, sha256=sha256_hex)
    db.commit()

    job_id = new_id("job")
    try:
        run_id = start_ingest(row.id, st.settings.prefect_api_url)
    except PrefectError:
        db.add(JobRow(id=job_id, chapter_id=row.id, flow_run_id=None, status="failed"))
        # Never leave 'pending' as if a run were queued: say it failed, and why (user-safe).
        db.execute(
            update(ChapterRow)
            .where(ChapterRow.id == row.id, ChapterRow.status == "pending")
            .values(status="failed", error=START_FAILED)
            .execution_options(synchronize_session=False)
        )
        audit(db, user.id, "ingest.start_failed", row.id, job_id=job_id)
        db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, START_FAILED) from None
    db.add(JobRow(id=job_id, chapter_id=row.id, flow_run_id=run_id, status="submitted"))
    audit(db, user.id, "ingest.start", row.id, job_id=job_id)
    db.commit()
    return JobAccepted(job_id=job_id)


def _hash_stream(f: IO[bytes], cap: int | None) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    while chunk := f.read(CHUNK):
        size += len(chunk)
        if cap is not None and size > cap:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, "file is larger than the upload limit"
            )
        h.update(chunk)
    return h.hexdigest(), size


def _media_url(st: AppState, user_id: str, ref: str) -> str:
    tok = sign_media(st.media_key, ref, user_id, st.settings.media_url_ttl_seconds)
    return f"/api/media/{tok}"


# ---------------------------------------------------------------------------------------------
# create and read chapters


@router.post(
    "/projects/{project_id}/chapters",
    response_model=ChapterOut,
    status_code=status.HTTP_201_CREATED,
)
def create_chapter(project_id: str, body: ChapterIn, user: CurrentUser, db: DB) -> ChapterOut:
    require_project_role(db, user, project_id, "editor")
    project = db.get(ProjectRow, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    clash = db.scalar(
        select(ChapterRow.id).where(
            ChapterRow.project_id == project_id, ChapterRow.number == body.number
        )
    )
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a chapter with this number exists")
    row = ChapterRow(
        id=new_id("chap"),
        project_id=project_id,
        number=body.number,
        title=body.title.strip() if body.title else None,
        language=body.language,
        format=project.format,
        source_kind=NO_SOURCE,
        source_ref="",
        source_sha256="",
        page_start=0,
        page_end=0,
        status="pending",
    )
    db.add(row)
    audit(db, user.id, "chapter.create", row.id, project_id=project_id)
    db.commit()
    return _out(row)


@router.get("/chapters/{chapter_id}", response_model=ChapterOut)
def get_chapter(chapter_id: str, user: CurrentUser, db: DB) -> ChapterOut:
    return _out(_chapter_for(db, user, chapter_id, "viewer"))


# ---------------------------------------------------------------------------------------------
# sources


class _BodyTooLarge(Exception):
    pass


class UploadSlots:
    """Non-blocking limit on concurrent uploads (each may spool up to the upload limit)."""

    def __init__(self, size: int) -> None:
        self._free = size
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._free <= 0:
                return False
            self._free -= 1
            return True

    def release(self) -> None:
        with self._lock:
            self._free += 1


def _capped(
    receive: Callable[[], Awaitable[Message]], cap: int
) -> Callable[[], Awaitable[Message]]:
    """Abort reading the request body once it passes ``cap`` (Content-Length is not trusted)."""
    seen = 0

    async def inner() -> Message:
        nonlocal seen
        msg = await receive()
        if msg["type"] == "http.request":
            seen += len(msg.get("body", b""))
            if seen > cap:
                raise _BodyTooLarge
        return msg

    return inner


def _form_range(value: object, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    if not isinstance(value, str) or not _DIGITS.match(value):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "page_start and page_end must be integers"
        )
    return int(value)


def _range(start: int | None, end: int | None) -> PageRange:
    if start is None or end is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "page_end is required for PDF and CBZ sources"
        )
    try:
        return PageRange(page_start=start, page_end=end)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            exc.errors(include_url=False, include_context=False, include_input=False),
        ) from exc


def _store_upload(
    st: AppState, row: ChapterRow, f: IO[bytes], start: int | None, end: int | None
) -> tuple[str, str, PageRange]:
    """Sniff, hash (streaming, capped) and store the upload. Runs in a worker thread."""
    cap = st.settings.max_upload_bytes
    if f.seek(0, os.SEEK_END) > cap:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, "file is larger than the upload limit"
        )
    f.seek(0)
    try:
        kind = sniff(f.read(SNIFF_BYTES), SOURCE_MIMES, SNIFF_BYTES)
    except UploadRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    _check_kind(row, kind)
    if kind.mime.startswith("image/"):  # a single image is exactly page 1
        rng = _range(1 if start is None else start, 1 if end is None else end)
        if rng.page_end != 1:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "a single image has only page 1"
            )
    else:
        rng = _range(1 if start is None else start, end)
    f.seek(0)
    sha256_hex, _ = _hash_stream(f, cap)
    f.seek(0)
    ref = st.store.put_fileobj("media", f, sha256_hex, kind.ext, kind.mime)
    return ref, sha256_hex, rng


@router.post(
    "/chapters/{chapter_id}/upload",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "page_start": {"type": "integer", "minimum": 1},
                            "page_end": {"type": "integer", "minimum": 1},
                        },
                    }
                }
            },
        }
    },
)
async def upload_source(
    chapter_id: str, request: Request, user: CurrentUser, db: DB
) -> JobAccepted:
    """Multipart upload (``file``, optional ``page_start`` / ``page_end``), at most the limit."""
    st = state(request)
    row = await run_in_threadpool(_prepare, db, user, chapter_id)
    slots: UploadSlots = request.app.state.upload_slots
    if not slots.try_acquire():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UPLOADS_BUSY)
    try:
        ref, sha256_hex, rng = await _receive_upload(request, st, row)
    except OSError as exc:  # spool volume full or failing: never a bare 500
        log.warning("upload spooling failed: %s", type(exc).__name__)
        await run_in_threadpool(_audit_spool_failure, db, user, row.id, exc)
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, SPOOL_FAILED) from None
    finally:
        slots.release()
    return await run_in_threadpool(
        lambda: _set_source_and_start(
            db, st, user, row, kind="upload", ref=ref, sha256_hex=sha256_hex, rng=rng
        )
    )


async def _receive_upload(
    request: Request, st: AppState, row: ChapterRow
) -> tuple[str, str, PageRange]:
    """Parse the capped multipart body (spooled under TMPDIR), then sniff, hash and store."""
    limited = Request(
        request.scope, _capped(request.receive, st.settings.max_upload_bytes + FORM_SLACK)
    )
    try:
        form = await limited.form(max_files=1, max_fields=2)
    except _BodyTooLarge:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, "file is larger than the upload limit"
        ) from None
    try:
        start = _form_range(form.get("page_start"), None)
        end = _form_range(form.get("page_end"), None)
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "a file is required")
        if start is not None and end is not None:
            _range(start, end)  # malformed or reversed ranges fail before any work
        return await run_in_threadpool(_store_upload, st, row, upload.file, start, end)
    finally:
        await form.close()


def _audit_spool_failure(db: Session, user: User, chapter_id: str, exc: OSError) -> None:
    audit(db, user.id, "upload.spool_failed", chapter_id, errno=exc.errno)
    db.commit()


def _listed(root: str, name: str, *, want_dir: bool) -> str | None:
    """The path of the listing entry of ``root`` named exactly ``name``, if it has the wanted
    type. Symlinks are never followed; the path is the listing's own, not built from input."""
    try:
        with os.scandir(root) as it:
            for e in it:
                if e.name != name:
                    continue
                kind_ok = e.is_dir if want_dir else e.is_file
                return e.path if kind_ok(follow_symlinks=False) else None
    except OSError:
        log.warning("imports directory is not readable")
    return None


def _open_import(root: str, project_id: str, name: str) -> int | None:
    """An O_RDONLY fd for the regular file ``name`` in the project's own imports directory
    (``<root>/<project_id>/``), else None.

    Imports are scoped per project, so an editor of one project can neither use nor probe a
    source staged for another. ``project_id`` is server-generated and re-checked against the
    id pattern; the user's ``name`` is only compared with names from the listing.
    """
    if not PROJECT_ID.match(project_id):
        return None
    folder = _listed(root, project_id, want_dir=True)
    path = None if folder is None else _listed(folder, name, want_dir=False)
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


@router.post(
    "/chapters/{chapter_id}/import",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def import_source(
    chapter_id: str, body: ImportIn, request: Request, user: CurrentUser, db: DB
) -> JobAccepted:
    """Use a file from the project's read-only imports directory (sources over the upload
    limit), ``<imports>/<project_id>/<name>``."""
    st = state(request)
    row = _prepare(db, user, chapter_id)
    fd = _open_import(st.settings.imports_dir, row.project_id, body.name)
    if fd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    with os.fdopen(fd, "rb") as f:
        try:
            kind = sniff(f.read(SNIFF_BYTES), SOURCE_MIMES, SNIFF_BYTES)
        except UploadRejected as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        _check_kind(row, kind)
        f.seek(0)
        sha256_hex, _ = _hash_stream(f, None)  # the flow enforces per-type size limits
    rng = PageRange(page_start=body.page_start, page_end=body.page_end)
    return _set_source_and_start(
        db, st, user, row, kind="import", ref=body.name, sha256_hex=sha256_hex, rng=rng
    )


# ---------------------------------------------------------------------------------------------
# understand


def _full_run_allowed(db: Session, row: ChapterRow) -> bool:
    """A finished trial, or a run the budget stopped, newer than the chapter's last ingest start
    (the rule lives in ``full_run_allowed``, shared with the flow's own re-check)."""
    return full_run_allowed(db.get(StageRunRow, (row.id, UNDERSTAND)), row.ingest_started_at)


def _claim_pending(db: Session, chapter_id: str) -> bool:
    """Conditional upsert of the stage row to 'pending'; False while a live run holds it."""
    key = (StageRunRow.chapter_id == chapter_id, StageRunRow.stage == UNDERSTAND)
    if db.scalar(select(StageRunRow.stage).where(*key)) is None:
        try:
            with db.begin_nested():
                db.add(StageRunRow(chapter_id=chapter_id, stage=UNDERSTAND, status="pending"))
        except IntegrityError:
            pass  # a concurrent start inserted it; the guarded UPDATE decides
    result = cast(
        CursorResult[Any],
        db.execute(
            update(StageRunRow)
            .where(*key, stage_takeover_allowed(ingest_stale_before()))
            .values(status="pending", error=None)  # counts and finished_at keep the trial
            .execution_options(synchronize_session=False)
        ),
    )
    return result.rowcount == 1


@router.post(
    "/chapters/{chapter_id}/understand",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_understand(
    chapter_id: str,
    request: Request,
    user: CurrentUser,
    db: DB,
    body: UnderstandIn | None = None,
) -> JobAccepted:
    """Start understanding: a trial (``limit_panels``) or, once a trial is done, a full run."""
    st = state(request)
    body = body or UnderstandIn()
    row = _chapter_for(db, user, chapter_id, "editor")
    if row.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_READY)
    _require_rights(db, user, row.project_id)  # local processing: render right, no cloud gate
    if body.limit_panels is None and not (body.confirm_full and _full_run_allowed(db, row)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, TRIAL_REQUIRED)
    if not _claim_pending(db, row.id):
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, UNDERSTAND_RUNNING)
    audit(db, user.id, "understand.start", row.id, limit_panels=body.limit_panels)
    db.commit()

    params: dict[str, str | int] = {"chapter_id": row.id}
    if body.limit_panels is not None:
        params["limit_panels"] = body.limit_panels
    job_id = new_id("job")
    try:
        run_id = start_deployment(UNDERSTAND_DEPLOYMENT, params, st.settings.prefect_api_url)
    except PrefectError:
        db.add(
            JobRow(
                id=job_id, chapter_id=row.id, flow_run_id=None, status="failed", stage=UNDERSTAND
            )
        )
        # Never leave 'pending' as if a run were queued (a run that already claimed it is kept).
        db.execute(
            update(StageRunRow)
            .where(
                StageRunRow.chapter_id == row.id,
                StageRunRow.stage == UNDERSTAND,
                StageRunRow.status == "pending",
            )
            .values(status="failed", error=UNDERSTAND_START_FAILED)
            .execution_options(synchronize_session=False)
        )
        audit(db, user.id, "understand.start_failed", row.id, job_id=job_id)
        db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, UNDERSTAND_START_FAILED) from None
    db.add(
        JobRow(
            id=job_id, chapter_id=row.id, flow_run_id=run_id, status="submitted", stage=UNDERSTAND
        )
    )
    audit(db, user.id, "understand.started", row.id, job_id=job_id)
    db.commit()
    return JobAccepted(job_id=job_id)


def _safe_counts(raw: dict[str, Any] | None) -> dict[str, bool | int | float]:
    out: dict[str, bool | int | float] = {}
    for key, public in SAFE_COUNTS.items():
        v = (raw or {}).get(key)
        if isinstance(v, bool | int | float):
            out[public] = v
    return out


@router.get("/chapters/{chapter_id}/stages", response_model=list[StageOut])
def list_stages(chapter_id: str, user: CurrentUser, db: DB) -> list[StageOut]:
    """Per-stage run state of a chapter (viewers). Counts are a fixed, numeric subset."""
    row = _chapter_for(db, user, chapter_id, "viewer")
    out: list[StageOut] = []
    stale_before = ingest_stale_before()
    for st in db.scalars(
        select(StageRunRow).where(StageRunRow.chapter_id == row.id).order_by(StageRunRow.stage)
    ):
        live = stage_alive(st, stale_before)
        out.append(
            StageOut(
                stage=st.stage,
                status=st.status,
                started_at=st.started_at,
                heartbeat_at=st.heartbeat_at,
                finished_at=st.finished_at,
                error=None if st.error is None else st.error[:300],
                counts=_safe_counts(st.counts),
                live=live,
                interrupted=st.status == "running" and not live,
            )
        )
    return out


# ---------------------------------------------------------------------------------------------
# results


def _load(model: type[Any], artifact: dict[str, Any]) -> Any:
    return model.model_validate_json(json.dumps(artifact))


Limit = Annotated[int, Query(ge=1, le=1000)]
Cursor = Annotated[str | None, Query(min_length=1, max_length=512)]
BAD_CURSOR = "invalid cursor"


def _encode_cursor(key: int | str, row_id: str) -> str:
    raw = json.dumps([key, row_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[object, str]:
    """Opaque keyset cursor -> (sort key, id). Malformed -> 422. The caller checks key types."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CURSOR) from None
    if not (
        isinstance(data, list)
        and len(data) == 2
        and isinstance(data[1], str)
        and 0 < len(data[1]) <= 80
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CURSOR)
    return data[0], data[1]


class PagesOut(BaseModel):
    items: list[PageOut]
    next: str | None  # pass as ?cursor= for the following page; null at the end


@router.get("/chapters/{chapter_id}/pages", response_model=PagesOut)
def list_pages(
    chapter_id: str,
    request: Request,
    user: CurrentUser,
    db: DB,
    limit: Limit = 200,
    cursor: Cursor = None,
) -> PagesOut:
    """Pages in reading order, keyset-paginated on (index, id)."""
    st = state(request)
    row = _chapter_for(db, user, chapter_id, "viewer")
    q = select(PageRow).where(PageRow.chapter_id == row.id)
    if cursor is not None:
        key, after_id = _decode_cursor(cursor)
        if type(key) is not int:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CURSOR)
        q = q.where(or_(PageRow.index > key, and_(PageRow.index == key, PageRow.id > after_id)))
    rows = list(db.scalars(q.order_by(PageRow.index, PageRow.id).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    out: list[PageOut] = []
    for prow in rows:
        page: Page = _load(Page, prow.artifact)
        out.append(
            PageOut(
                id=page.id,
                index=page.index,
                source_page=page.source_page,
                source_y0=page.source_y0,
                source_y1=page.source_y1,
                width=page.width,
                height=page.height,
                image_url=_media_url(st, user.id, page.image_ref),
            )
        )
    nxt = _encode_cursor(rows[-1].index, rows[-1].id) if more else None
    return PagesOut(items=out, next=nxt)


def _cited(c: Any) -> CitedOut:
    return CitedOut(value=None if c.value is None else str(c.value), panel_ids=list(c.panel_ids))


def _scenes(db: Session, panel_ids: list[str]) -> dict[str, SceneOut]:
    """Validated scenes by panel id. A row that no longer validates is hidden, not leaked."""
    out: dict[str, SceneOut] = {}
    if not panel_ids:
        return out
    for srow in db.scalars(select(SceneRow).where(SceneRow.panel_id.in_(panel_ids))):
        try:
            sc: SceneJSON = _load(SceneJSON, srow.artifact)
        except ValidationError:
            log.warning("a stored scene failed validation and is not shown")
            continue
        if sc.panel_id != srow.panel_id:
            continue
        out[srow.panel_id] = SceneOut(
            setting=_cited(sc.setting),
            action=_cited(sc.action),
            shot_hint=_cited(sc.shot_hint),
            time_of_day=_cited(sc.time_of_day),
            weather=_cited(sc.weather),
            figures=[
                FigureOut(description=_cited(f.description), emotion=_cited(f.emotion))
                for f in sc.figures
            ],
            unknown_fields=sc.unknown_fields,
        )
    return out


@router.get("/pages/{page_id}/panels", response_model=list[PanelOut])
def list_panels(page_id: str, request: Request, user: CurrentUser, db: DB) -> list[PanelOut]:
    """Panels in reading order, each with its bubbles in reading order."""
    st = state(request)
    prow = db.get(PageRow, page_id)
    if prow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "page not found")
    try:
        _chapter_for(db, user, prow.chapter_id, "viewer")
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "page not found") from None
        raise
    panel_rows = list(
        db.scalars(
            select(PanelRow).where(PanelRow.page_id == prow.id).order_by(PanelRow.reading_order)
        )
    )
    by_panel: dict[str, list[BubbleOut]] = {p.id: [] for p in panel_rows}
    if panel_rows:
        for brow in db.scalars(
            select(BubbleRow)
            .where(BubbleRow.panel_id.in_(list(by_panel)))
            .order_by(BubbleRow.panel_id, BubbleRow.reading_order)
        ):
            b: Bubble = _load(Bubble, brow.artifact)
            by_panel[brow.panel_id].append(
                BubbleOut(
                    id=b.id,
                    reading_order=b.reading_order,
                    bbox=b.bbox,
                    kind=b.kind,
                    text=b.text,
                    lang=b.lang,
                    ocr_confidence=b.ocr_confidence,
                    ocr_engine=b.ocr_engine,
                    needs_review=brow.needs_review,
                )
            )
    scenes = _scenes(db, [p.id for p in panel_rows])
    out: list[PanelOut] = []
    for row in panel_rows:
        panel: Panel = _load(Panel, row.artifact)
        out.append(
            PanelOut(
                id=panel.id,
                reading_order=panel.reading_order,
                bbox=panel.bbox,
                polygon=panel.polygon,
                crop_url=_media_url(st, user.id, panel.crop_ref),
                bubbles=by_panel[row.id],
                scene=scenes.get(row.id),
            )
        )
    return out


class ReviewItemsOut(BaseModel):
    items: list[ReviewItemOut]
    next: str | None  # pass as ?cursor= for the following page; null at the end


@router.get("/projects/{project_id}/review-items", response_model=ReviewItemsOut)
def list_review_items(
    project_id: str,
    user: CurrentUser,
    db: DB,
    status_: Annotated[Literal["open", "resolved"], Query(alias="status")] = "open",
    chapter_id: Annotated[str | None, Query(max_length=80)] = None,
    limit: Limit = 200,
    cursor: Cursor = None,
) -> ReviewItemsOut:
    """Review items oldest first, keyset-paginated on (created_at, id)."""
    require_project_role(db, user, project_id, "viewer")
    q = select(ReviewItemRow).where(
        ReviewItemRow.project_id == project_id, ReviewItemRow.status == status_
    )
    if chapter_id is not None:
        q = q.where(ReviewItemRow.chapter_id == chapter_id)
    if cursor is not None:
        key, after_id = _decode_cursor(cursor)
        try:
            after = aware(datetime.fromisoformat(key)) if isinstance(key, str) else None
        except ValueError:
            after = None
        if after is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CURSOR)
        q = q.where(
            or_(
                ReviewItemRow.created_at > after,
                and_(ReviewItemRow.created_at == after, ReviewItemRow.id > after_id),
            )
        )
    q = q.order_by(ReviewItemRow.created_at, ReviewItemRow.id).limit(limit + 1)
    rows = list(db.scalars(q))
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        ReviewItemOut(
            id=r.id,
            chapter_id=r.chapter_id,
            artifact_id=r.artifact_id,
            kind=r.kind,
            reason=r.reason,
            status=r.status,
            created_at=r.created_at,
        )
        for r in rows
    ]
    last = rows[-1] if more else None
    nxt = _encode_cursor(aware(last.created_at).isoformat(), last.id) if last else None
    return ReviewItemsOut(items=items, next=nxt)
