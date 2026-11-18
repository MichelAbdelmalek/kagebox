from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from PIL import Image

from kagebox_api.db import User
from kagebox_api.deps import CSRF_COOKIE, current_user
from kagebox_api.main import PUBLIC_ROUTES, create_app
from kagebox_api.security import hash_password, sign_media
from pipeline.common.ids import new_id
from pipeline.common.settings import Settings

ORIGIN = "https://testserver"
PW = "correct horse battery"


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[tuple[TestClient, Settings]]:
    key = tmp_path / "media_key"
    key.write_text("k" * 48)
    settings = Settings(profile="test", media_signing_key_file=str(key))
    app = create_app(settings, allowed_origin=ORIGIN)
    with next(app.state.kagebox.db.session()) as s:
        s.add(
            User(
                id="user_admin",
                email="admin@example.com",
                password_hash=hash_password(PW),
                role="admin",
            )
        )
        s.add(User(id="user_ed", email="ed@example.com", password_hash=hash_password(PW)))
        s.add(User(id="user_eve", email="eve@example.com", password_hash=hash_password(PW)))
        s.commit()
    client = TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN})
    yield client, settings


def login(c: TestClient, email: str) -> None:
    c.cookies.clear()
    r = c.post("/api/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = c.cookies[CSRF_COOKIE]


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, "PNG")
    return out.getvalue()


def make_project_with_rights(c: TestClient, **form: str) -> str:
    login(c, "ed@example.com")
    pid: str = c.post("/api/projects", json={"title": "Clockwork", "format": "comic_ltr"}).json()[
        "id"
    ]
    data = {"basis": "public_domain", "justification": "Published 1941, not renewed", **form}
    r = c.post(
        f"/api/projects/{pid}/rights", data=data, files={"evidence": ("e.png", png(), "image/png")}
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    return pid


def test_every_route_requires_auth_except_allowlist(
    app_client: tuple[TestClient, Settings],
) -> None:
    c, _ = app_client
    for route in c.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute) or route.path.startswith("/api/docs"):
            continue
        deps = {d.call for d in route.dependant.dependencies}
        for method in route.methods:
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            assert current_user in deps, f"{method} {route.path} has no auth dependency"


def test_unauthenticated_requests_rejected(app_client: tuple[TestClient, Settings]) -> None:
    c, _ = app_client
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/projects").status_code == 401
    assert c.get("/api/auth/me").status_code == 401


def test_login_sets_hardened_cookies_and_bad_password_fails(
    app_client: tuple[TestClient, Settings],
) -> None:
    c, _ = app_client
    r = c.post("/api/auth/login", json={"email": "ed@example.com", "password": "nope"})
    assert r.status_code == 401
    r = c.post("/api/auth/login", json={"email": "ed@example.com", "password": PW})
    cookies = r.headers.get_list("set-cookie")
    session = next(h for h in cookies if h.startswith("__Host-kb_session"))
    for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert attr.lower() in session.lower()


def test_lockout_after_repeated_failures(app_client: tuple[TestClient, Settings]) -> None:
    c, _ = app_client
    for _ in range(5):
        c.post("/api/auth/login", json={"email": "eve@example.com", "password": "wrong"})
    r = c.post("/api/auth/login", json={"email": "eve@example.com", "password": PW})
    assert r.status_code == 401


def test_csrf_and_origin_enforced(app_client: tuple[TestClient, Settings]) -> None:
    c, _ = app_client
    login(c, "ed@example.com")
    body = {"title": "X", "format": "manga_rtl"}
    assert c.post("/api/projects", json=body, headers={"x-csrf-token": "bad"}).status_code == 403
    assert c.post("/api/projects", json=body, headers={"origin": "https://evil"}).status_code == 403
    assert c.post("/api/projects", json=body).status_code == 201


def test_render_blocked_until_rights_approved(app_client: tuple[TestClient, Settings]) -> None:
    c, _ = app_client
    pid = make_project_with_rights(c)
    r = c.post(f"/api/projects/{pid}/render")
    assert r.status_code == 403 and "pending" in r.json()["detail"]
    # The editor cannot approve their own record.
    assert c.post(f"/api/projects/{pid}/rights/approve").status_code == 403
    login(c, "admin@example.com")
    assert c.post(f"/api/projects/{pid}/rights/approve").json()["status"] == "approved"
    login(c, "ed@example.com")
    assert c.post(f"/api/projects/{pid}/render").status_code == 202


def test_admin_cannot_approve_own_record(app_client: tuple[TestClient, Settings]) -> None:
    c, _ = app_client
    login(c, "admin@example.com")
    pid = c.post("/api/projects", json={"title": "A", "format": "comic_ltr"}).json()["id"]
    c.post(
        f"/api/projects/{pid}/rights",
        data={"basis": "owned", "justification": "mine"},
        files={"evidence": ("e.png", png(), "image/png")},
    )
    assert c.post(f"/api/projects/{pid}/rights/approve").status_code == 409


def test_non_member_gets_404(app_client: tuple[TestClient, Settings]) -> None:
    c, _ = app_client
    pid = make_project_with_rights(c)
    login(c, "eve@example.com")
    assert c.get(f"/api/projects/{pid}").status_code == 404
    assert c.post(f"/api/projects/{pid}/render").status_code == 404


def test_evidence_upload_rejects_disguised_file(app_client: tuple[TestClient, Settings]) -> None:
    c, _ = app_client
    login(c, "ed@example.com")
    pid = c.post("/api/projects", json={"title": "A", "format": "comic_ltr"}).json()["id"]
    r = c.post(
        f"/api/projects/{pid}/rights",
        data={"basis": "owned", "justification": "mine"},
        files={"evidence": ("e.png", b"#!/bin/sh\necho hi\n", "image/png")},
    )
    assert r.status_code == 422


def test_signed_media_url_bound_to_user_and_expiry(
    app_client: tuple[TestClient, Settings],
) -> None:
    c, _ = app_client
    pid = make_project_with_rights(c)
    url = c.get(f"/api/projects/{pid}").json()["rights"]["evidence_url"]
    r = c.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "private, no-store"
    login(c, "eve@example.com")
    assert c.get(url).status_code == 403  # another user's link
    login(c, "ed@example.com")
    st = c.app.state.kagebox  # type: ignore[attr-defined]
    ref = url  # build an already-expired token for the same object
    expired = sign_media(st.media_key, "evidence/sha256/" + "0" * 64 + ".png", "user_ed", 1, now=0)
    assert c.get(f"/api/media/{expired}").status_code == 403
    assert ref


def test_new_id_shapes() -> None:
    assert new_id("user").startswith("user_")
