"""Record which rows each transaction locks (``Session.get(..., with_for_update=True)``).

SQLite ignores FOR UPDATE, so the lock order cannot be observed in the database; the code under
test takes its row locks through ``Session.get``, which this wraps. The agreed order for every
transaction that touches both rows is: chapter row first, then the stage row.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from pipeline.common.dbmodels import ChapterRow, StageRunRow


def record_row_locks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Session, str]]:
    """Patch ``Session.get``; the returned list gets (session, 'chapter' | 'stage') per lock."""
    events: list[tuple[Session, str]] = []  # sessions kept alive so ids are never reused
    real_get = Session.get

    def get(self: Session, entity: type, ident: object, **kw: object) -> object:
        if kw.get("with_for_update"):
            if entity is ChapterRow:
                events.append((self, "chapter"))
            elif entity is StageRunRow:
                events.append((self, "stage"))
        return real_get(self, entity, ident, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Session, "get", get)
    return events


def violations(events: list[tuple[Session, str]]) -> list[int]:
    """Ids of sessions that locked the stage row before the chapter row."""
    seen: dict[int, list[str]] = {}
    for session, kind in events:
        seen.setdefault(id(session), []).append(kind)
    return [
        sid
        for sid, kinds in seen.items()
        if "stage" in kinds and "chapter" in kinds and kinds.index("stage") < kinds.index("chapter")
    ]


def both_locked(events: list[tuple[Session, str]]) -> int:
    """How many transactions locked both rows (a sanity floor so the check cannot pass empty)."""
    seen: dict[int, set[str]] = {}
    for session, kind in events:
        seen.setdefault(id(session), set()).add(kind)
    return sum(1 for kinds in seen.values() if kinds == {"chapter", "stage"})
