"""Request dependencies: DB session, authenticated user, CSRF and project roles."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from kagebox_api.db import Database, ProjectMember, SessionRow, User, aware, utcnow
from kagebox_api.security import token_hash
from pipeline.common.settings import Settings
from pipeline.common.storage import ObjectStore

SESSION_COOKIE = "__Host-kb_session"
CSRF_COOKIE = "__Host-kb_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass
class AppState:
    settings: Settings
    db: Database
    store: ObjectStore
    media_key: bytes


def state(request: Request) -> AppState:
    st: AppState = request.app.state.kagebox
    return st


def get_db(request: Request) -> Iterator[Session]:
    yield from state(request).db.session()


DB = Annotated[Session, Depends(get_db)]


def current_user(request: Request, db: DB) -> User:
    """Authenticate via the session cookie and enforce CSRF on state-changing requests."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    row = db.get(SessionRow, token_hash(token))
    if row is None or aware(row.expires_at) <= utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(token_hash(sent), row.csrf_hash):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    request.state.session_row = row
    return user


CurrentUser = Annotated[User, Depends(current_user)]

ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def require_project_role(db: Session, user: User, project_id: str, minimum: str) -> str:
    """Admins can see everything; everyone else needs a membership of at least ``minimum``.

    Returns 404 (not 403) for non-members so project ids cannot be probed.
    """
    if user.role == "admin":
        return "owner"
    m = db.get(ProjectMember, (project_id, user.id))
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    if ROLE_RANK[m.role] < ROLE_RANK[minimum]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"needs {minimum} role")
    return m.role
