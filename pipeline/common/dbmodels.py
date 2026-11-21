"""ORM models shared by the API and the workers (Postgres in compose, SQLite in unit tests)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    ColumnElement,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSON,
    }


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin | member
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SessionRow(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProjectRow(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16))  # owner | editor | viewer


class RightsRow(Base):
    __tablename__ = "rights_records"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    record: Mapped[dict[str, Any]]  # RightsRecord JSON (validated on every read and write)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(120))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)


# An 'ingesting' chapter whose owning run has not sent a heartbeat for this long (or, before its
# first heartbeat, started this long ago) is presumed dead: it may be taken over by a new flow run
# or by the API accepting a new source, and the API reports it as interrupted. The run beats at
# start and after every page. Shared by orchestration and the API.
STALE_INGEST_S = 600


class ChapterRow(Base):
    __tablename__ = "chapters"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    language: Mapped[str] = mapped_column(String(8))
    format: Mapped[str] = mapped_column(String(32))
    source_kind: Mapped[str] = mapped_column(String(16))  # upload | import
    source_ref: Mapped[str] = mapped_column(String(300))
    source_sha256: Mapped[str] = mapped_column(String(64))
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # pending | ingesting | ready | failed
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Set when a run takes the chapter to 'ingesting'; a run older than the stale limit may be
    # taken over, and a run only writes results while it still owns the chapter.
    ingest_started_at: Mapped[datetime | None] = mapped_column(default=None)
    # The owning run (its Prefect flow-run id, or a fresh token for direct calls).
    ingest_run_id: Mapped[str | None] = mapped_column(String(80), default=None)
    # Last sign of life of the owning run (at start and after every page); staleness uses it.
    ingest_heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PageRow(Base):
    __tablename__ = "pages"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    index: Mapped[int] = mapped_column(Integer)  # 1-based reading position within the chapter
    artifact: Mapped[dict[str, Any]]  # Page JSON


class PanelRow(Base):
    __tablename__ = "panels"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    page_id: Mapped[str] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    reading_order: Mapped[int] = mapped_column(Integer)  # 1-based
    artifact: Mapped[dict[str, Any]]  # Panel JSON


class BubbleRow(Base):
    __tablename__ = "bubbles"
    __table_args__ = (
        UniqueConstraint("panel_id", "reading_order", name="uq_bubbles_panel_id_reading_order"),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    panel_id: Mapped[str] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    reading_order: Mapped[int] = mapped_column(Integer)  # 1-based, within the panel
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    artifact: Mapped[dict[str, Any]]  # Bubble JSON


class ReviewItemRow(Base):
    __tablename__ = "review_items"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    artifact_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | resolved
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SceneRow(Base):
    """One SceneJSON per panel (the understand stage's output); replaced on a re-run."""

    __tablename__ = "scenes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    panel_id: Mapped[str] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), unique=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    params_hash: Mapped[str] = mapped_column(String(64))
    artifact: Mapped[dict[str, Any]]  # Scene JSON
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class StageRunRow(Base):
    """Per-(chapter, stage) run lock: owner, heartbeat and outcome of the latest run."""

    __tablename__ = "stage_runs"
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(String(16), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))  # pending | running | done | failed
    run_id: Mapped[str | None] = mapped_column(String(80), default=None)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    counts: Mapped[dict[str, Any] | None] = mapped_column(default=None)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    flow_run_id: Mapped[str | None] = mapped_column(String(80), default=None)
    status: Mapped[str] = mapped_column(String(16))
    # The pipeline stage the run belongs to (ingest, understand, ...).
    stage: Mapped[str] = mapped_column(String(16), default="ingest", server_default="ingest")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


def aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive datetimes from the DB as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def ingest_stale_before(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(seconds=STALE_INGEST_S)


def ingest_takeover_allowed(stale_before: datetime) -> ColumnElement[bool]:
    """SQL: the chapter is not held by a live run (not 'ingesting', or no recent heartbeat)."""
    last = func.coalesce(ChapterRow.ingest_heartbeat_at, ChapterRow.ingest_started_at)
    return or_(ChapterRow.status != "ingesting", last.is_(None), last < stale_before)


def ingest_alive(row: ChapterRow, stale_before: datetime) -> bool:
    """Python twin of ``not ingest_takeover_allowed``: 'ingesting' with a recent sign of life."""
    last = row.ingest_heartbeat_at or row.ingest_started_at
    return row.status == "ingesting" and last is not None and aware(last) >= stale_before


def stage_takeover_allowed(stale_before: datetime) -> ColumnElement[bool]:
    """SQL: the stage run is not held by a live run (not 'running', or no recent heartbeat)."""
    last = func.coalesce(StageRunRow.heartbeat_at, StageRunRow.started_at)
    return or_(StageRunRow.status != "running", last.is_(None), last < stale_before)


def stage_alive(row: StageRunRow, stale_before: datetime) -> bool:
    """Python twin of ``not stage_takeover_allowed``: 'running' with a recent sign of life."""
    last = row.heartbeat_at or row.started_at
    return row.status == "running" and last is not None and aware(last) >= stale_before


def claim_stage(session: Session, chapter_id: str, stage: str, run_id: str, now: datetime) -> bool:
    """Take the (chapter, stage) lock for ``run_id``; True only if this run now owns it.

    Inserts a 'pending' row when none exists (a concurrent insert losing the race is ignored),
    then one conditional UPDATE to 'running' guarded by ``stage_takeover_allowed``. The caller
    commits. A live run elsewhere (same rule and window as the ingest lock) refuses the claim.
    """
    key = (StageRunRow.chapter_id == chapter_id) & (StageRunRow.stage == stage)
    if session.scalar(select(StageRunRow.stage).where(key)) is None:
        try:
            with session.begin_nested():
                session.add(StageRunRow(chapter_id=chapter_id, stage=stage, status="pending"))
        except IntegrityError:
            pass  # another run inserted it first; the guarded UPDATE decides
    result = session.execute(
        update(StageRunRow)
        .where(key, stage_takeover_allowed(ingest_stale_before(now)))
        .values(
            status="running",
            run_id=run_id,
            started_at=now,
            heartbeat_at=now,
            finished_at=None,
            error=None,
            counts=None,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(getattr(result, "rowcount", 0) == 1)
