from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from kagebox_api.db import SessionRow, User, audit, aware, utcnow
from kagebox_api.deps import CSRF_COOKIE, DB, SESSION_COOKIE, CurrentUser, state
from kagebox_api.security import new_token, token_hash, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_FAILED = 5
LOCKOUT = timedelta(minutes=15)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    id: str
    email: str
    role: str


@router.post("/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response, db: DB) -> UserOut:
    st = state(request)
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    now = utcnow()
    locked = user is not None and user.locked_until is not None and aware(user.locked_until) > now
    ok = verify_password(user.password_hash if user else None, body.password)
    if user is None or locked or not ok:
        if user is not None and not locked:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED:
                user.locked_until = now + LOCKOUT
                user.failed_logins = 0
            audit(db, user.id, "auth.login_failed", user.id)
            db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "email or password is incorrect")

    user.failed_logins = 0
    user.locked_until = None
    token, csrf = new_token(), new_token()
    ttl = timedelta(hours=st.settings.session_ttl_hours)
    db.add(
        SessionRow(
            token_hash=token_hash(token),
            user_id=user.id,
            csrf_hash=token_hash(csrf),
            expires_at=now + ttl,
        )
    )
    audit(db, user.id, "auth.login", user.id)
    db.commit()
    max_age = int(ttl.total_seconds())
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    # Readable by the web app's JS so it can echo it in the X-CSRF-Token header.
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, path="/", secure=True, httponly=False, samesite="strict"
    )
    return UserOut(id=user.id, email=user.email, role=user.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(user: CurrentUser, request: Request, response: Response, db: DB) -> None:
    row: SessionRow = request.state.session_row
    db.delete(row)
    audit(db, user.id, "auth.logout", user.id)
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, samesite="strict")


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, email=user.email, role=user.role)
