"""Relational metadata access for the API; the ORM models live in ``pipeline.common.dbmodels``."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pipeline.common.database import make_engine
from pipeline.common.dbmodels import (
    AuditEvent,
    Base,
    BubbleRow,
    ChapterRow,
    JobRow,
    PageRow,
    PanelRow,
    ProjectMember,
    ProjectRow,
    ReviewItemRow,
    RightsRow,
    SessionRow,
    User,
    aware,
    utcnow,
)

__all__ = [
    "AuditEvent",
    "Base",
    "BubbleRow",
    "ChapterRow",
    "Database",
    "JobRow",
    "PageRow",
    "PanelRow",
    "ProjectMember",
    "ProjectRow",
    "ReviewItemRow",
    "RightsRow",
    "SessionRow",
    "User",
    "audit",
    "aware",
    "make_engine",
    "utcnow",
]


class Database:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._factory = sessionmaker(engine, expire_on_commit=False)

    def session(self) -> Iterator[Session]:
        with self._factory() as s:
            yield s


def audit(db: Session, actor_id: str | None, action: str, target: str, **detail: Any) -> None:
    db.add(AuditEvent(actor_id=actor_id, action=action, target=target, detail=detail))
