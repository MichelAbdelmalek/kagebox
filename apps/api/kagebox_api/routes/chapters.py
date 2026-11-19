"""Chapters: create, add a source (upload or server-side import), start ingest, read results.

Everything a user sends is untrusted: uploads are capped while streaming and sniffed by content;
import names are only ever compared against a listing of the imports directory, never joined
into a path. Responses carry signed, user-bound media URLs, never object keys or file paths.
OCR text is returned as JSON data only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import IO, Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import CursorResult, select, update
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
    utcnow,
)
from kagebox_api.deps import DB, AppState, CurrentUser, require_project_role, state
from kagebox_api.prefect_client import PrefectError, start_ingest
from kagebox_api.security import sign_media
from pipeline.common.ids import new_id
from pipeline.common.schemas import (
    BBox,
    Bubble,
    Page,
    Panel,
    RightsDenied,
    RightsRecord,
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
_DIGITS = re.compile(r"^[0-9]{1,6}$")


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


class PanelOut(BaseModel):
    id: str
    reading_order: int
    bbox: BBox
    polygon: list[tuple[int, int]] | None
    crop_url: str
    bubbles: list[BubbleOut]


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
    return ChapterOut(
        id=row.id,
        project_id=row.project_id,
        number=row.number,
        title=row.title,
        language=row.language,
        format=row.format,
        status=row.status,
        error=row.error,
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


def _prepare(db: Session, user: User, chapter_id: str) -> ChapterRow:
    """Checks shared by upload and import, before any bytes are read."""
    row = _chapter_for(db, user, chapter_id, "editor")
    _require_rights(db, user, row.project_id)
    if row.status == "ingesting":
        raise HTTPException(status.HTTP_409_CONFLICT, "chapter is being ingested")
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
    result = cast(
        CursorResult[Any],
        db.execute(
            update(ChapterRow)
            .where(ChapterRow.id == row.id, ChapterRow.status != "ingesting")
            .values(
                source_kind=kind,
                source_ref=ref,
                source_sha256=sha256_hex,
                page_start=rng.page_start,
                page_end=rng.page_end,
                status="pending",
                error=None,
            )
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
        ref, sha256_hex, rng = await run_in_threadpool(
            _store_upload, st, row, upload.file, start, end
        )
    finally:
        await form.close()
    return await run_in_threadpool(
        lambda: _set_source_and_start(
            db, st, user, row, kind="upload", ref=ref, sha256_hex=sha256_hex, rng=rng
        )
    )


def _open_import(root: str, name: str) -> int | None:
    """An O_RDONLY fd for the listed regular file named exactly ``name``, else None.

    The user's string is only compared with names from the listing; the path opened is the
    listing's own entry, never built from user input. Symlinks are not followed.
    """
    try:
        with os.scandir(root) as it:
            match = next(
                (e for e in it if e.name == name and e.is_file(follow_symlinks=False)), None
            )
    except OSError:
        log.warning("imports directory is not readable")
        return None
    if match is None:
        return None
    try:
        fd = os.open(match.path, os.O_RDONLY | os.O_NOFOLLOW)
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
    """Use a file from the read-only imports directory (for sources over the upload limit)."""
    st = state(request)
    row = _prepare(db, user, chapter_id)
    fd = _open_import(st.settings.imports_dir, body.name)
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
# results


def _load(model: type[Any], artifact: dict[str, Any]) -> Any:
    return model.model_validate_json(json.dumps(artifact))


@router.get("/chapters/{chapter_id}/pages", response_model=list[PageOut])
def list_pages(chapter_id: str, request: Request, user: CurrentUser, db: DB) -> list[PageOut]:
    st = state(request)
    row = _chapter_for(db, user, chapter_id, "viewer")
    out: list[PageOut] = []
    for prow in db.scalars(
        select(PageRow).where(PageRow.chapter_id == row.id).order_by(PageRow.index)
    ):
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
            )
        )
    return out


@router.get("/projects/{project_id}/review-items", response_model=list[ReviewItemOut])
def list_review_items(
    project_id: str,
    user: CurrentUser,
    db: DB,
    status_: Annotated[Literal["open", "resolved"], Query(alias="status")] = "open",
    chapter_id: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[ReviewItemOut]:
    require_project_role(db, user, project_id, "viewer")
    q = select(ReviewItemRow).where(
        ReviewItemRow.project_id == project_id, ReviewItemRow.status == status_
    )
    if chapter_id is not None:
        q = q.where(ReviewItemRow.chapter_id == chapter_id)
    q = q.order_by(ReviewItemRow.created_at, ReviewItemRow.id).limit(limit)
    return [
        ReviewItemOut(
            id=r.id,
            chapter_id=r.chapter_id,
            artifact_id=r.artifact_id,
            kind=r.kind,
            reason=r.reason,
            status=r.status,
            created_at=r.created_at,
        )
        for r in db.scalars(q)
    ]
