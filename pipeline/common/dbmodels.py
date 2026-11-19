"""ORM models shared by the API and the workers (Postgres in compose, SQLite in unit tests)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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


# An 'ingesting' chapter whose run started longer ago than this may be taken over (by a new
# flow run, or by the API accepting a new source). Shared by orchestration and the API.
STALE_INGEST_S = 7200


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


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    flow_run_id: Mapped[str | None] = mapped_column(String(80), default=None)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


def aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo; treat naive datetimes from the DB as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
