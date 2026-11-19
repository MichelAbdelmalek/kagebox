"""Chapters API: create, upload / server-side import, start ingest via Prefect, read results."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from kagebox_api.db import (
    BubbleRow,
    ChapterRow,
    JobRow,
    PageRow,
    PanelRow,
    ProjectMember,
    ReviewItemRow,
    User,
)
from kagebox_api.deps import CSRF_COOKIE
from kagebox_api.main import create_app
from kagebox_api.security import hash_password
from pipeline.common.ids import new_id
from pipeline.common.schemas import Bubble, Page, Panel
from pipeline.common.settings import Settings
from pipeline.common.storage import MemoryObjectStore, make_ref_from_digest

ORIGIN = "https://testserver"
PW = "correct horse battery"
PREFECT = "http://prefect.test/api"
DEPLOYMENT_ID = "2d4d8ff5-9de9-42cc-ac2d-8e3077cfd240"
# Prefect has no name-based create route: look the deployment up by name, then create by id.
LOOKUP = f"{PREFECT}/deployments/name/chapter-ingest/chapter-ingest"
CREATE_RUN = f"{PREFECT}/deployments/{DEPLOYMENT_ID}/create_flow_run"
RUN_ID = "0b6f4a8e-5c1d-4f3a-9d1e-2a7b8c9d0e1f"
PDF = b"%PDF-1.7\n" + b"0" * 2048 + b"\n%%EOF\n"


@dataclass
class Env:
    c: TestClient
    store: MemoryObjectStore
    imports: Path
    settings: Settings

    def session(self) -> Any:
        return next(self.c.app.state.kagebox.db.session())  # type: ignore[attr-defined]


def _app(tmp_path: Path, **overrides: Any) -> Env:
    key = tmp_path / "media_key"
    key.write_text("k" * 48)
    imports = tmp_path / "imports"
    imports.mkdir(exist_ok=True)
    settings = Settings(
        profile="test",
        media_signing_key_file=str(key),
        imports_dir=str(imports),
        prefect_api_url=PREFECT,
        **overrides,
    )
    store = MemoryObjectStore()
    app = create_app(settings, store=store, allowed_origin=ORIGIN)
    with next(app.state.kagebox.db.session()) as s:
        s.add(
            User(
                id="user_admin",
                email="admin@example.com",
                password_hash=hash_password(PW),
                role="admin",
            )
        )
        for uid, email in (("user_ed", "ed"), ("user_vi", "vi"), ("user_eve", "eve")):
            s.add(User(id=uid, email=f"{email}@example.com", password_hash=hash_password(PW)))
        s.commit()
    c = TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN})
    return Env(c, store, imports, settings)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield _app(tmp_path)


def login(c: TestClient, who: str) -> None:
    c.cookies.clear()
    r = c.post("/api/auth/login", json={"email": f"{who}@example.com", "password": PW})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = c.cookies[CSRF_COOKIE]


def png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "PNG")
    return out.getvalue()


def project(e: Env, fmt: str = "manga_rtl", approve: bool = True) -> str:
    login(e.c, "ed")
    pid: str = e.c.post("/api/projects", json={"title": "Clockwork", "format": fmt}).json()["id"]
    r = e.c.post(
        f"/api/projects/{pid}/rights",
        data={"basis": "public_domain", "justification": "Published 1941, not renewed"},
        files={"evidence": ("e.png", png_bytes(), "image/png")},
    )
    assert r.status_code == 201, r.text
    with e.session() as s:
        s.add(ProjectMember(project_id=pid, user_id="user_vi", role="viewer"))
        s.commit()
    if approve:
        login(e.c, "admin")
        assert e.c.post(f"/api/projects/{pid}/rights/approve").status_code == 200
        login(e.c, "ed")
    return pid


def staged(e: Env, pid: str) -> Path:
    """The project's own import directory (imports are scoped per project)."""
    d = e.imports / pid
    d.mkdir(exist_ok=True)
    return d


def chapter(e: Env, pid: str, number: int = 1) -> str:
    r = e.c.post(f"/api/projects/{pid}/chapters", json={"number": number, "language": "ja"})
    assert r.status_code == 201, r.text
    cid: str = r.json()["id"]
    return cid


def chapter_row(e: Env, cid: str) -> ChapterRow:
    with e.session() as s:
        row = s.get(ChapterRow, cid)
        assert row is not None
        return row  # type: ignore[no-any-return]


def ok_lookup(mock: respx.MockRouter) -> respx.Route:
    return mock.get(LOOKUP).mock(
        return_value=httpx.Response(200, json={"id": DEPLOYMENT_ID, "name": "chapter-ingest"})
    )


def ok_prefect(mock: respx.MockRouter) -> respx.Route:
    ok_lookup(mock)
    return mock.post(CREATE_RUN).mock(
        return_value=httpx.Response(201, json={"id": RUN_ID, "name": "x"})
    )


# ---------------------------------------------------------------------------------------------
# create / read


def test_create_chapter_editor_only(env: Env) -> None:
    pid = project(env)
    r = env.c.post(
        f"/api/projects/{pid}/chapters", json={"number": 3, "title": "Ch 3", "language": "ja"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending" and body["format"] == "manga_rtl"
    assert body["source_kind"] is None and body["page_start"] is None
    assert "source_ref" not in body and "ingest_run_id" not in body
    assert env.c.get(f"/api/chapters/{body['id']}").json()["number"] == 3
    # Same number twice in one project is refused.
    dup = {"number": 3, "language": "ja"}
    assert env.c.post(f"/api/projects/{pid}/chapters", json=dup).status_code == 409
    for bad in ({"number": 0, "language": "ja"}, {"number": 1, "language": "fr"}, {"number": 1}):
        assert env.c.post(f"/api/projects/{pid}/chapters", json=bad).status_code == 422
    login(env.c, "vi")
    new = {"number": 4, "language": "ja"}
    assert env.c.post(f"/api/projects/{pid}/chapters", json=new).status_code == 403
    assert env.c.get(f"/api/chapters/{body['id']}").status_code == 200  # viewers read
    login(env.c, "eve")
    assert env.c.post(f"/api/projects/{pid}/chapters", json=new).status_code == 404
    r = env.c.get(f"/api/chapters/{body['id']}")
    assert r.status_code == 404 and r.json()["detail"] == "chapter not found"
    assert env.c.get("/api/chapters/chap_nope").json()["detail"] == "chapter not found"


def test_every_new_route_requires_auth(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    env.c.cookies.clear()
    calls = [
        ("POST", f"/api/projects/{pid}/chapters"),
        ("GET", f"/api/chapters/{cid}"),
        ("POST", f"/api/chapters/{cid}/upload"),
        ("POST", f"/api/chapters/{cid}/import"),
        ("GET", f"/api/chapters/{cid}/pages"),
        ("GET", "/api/pages/page_x/panels"),
        ("GET", f"/api/projects/{pid}/review-items"),
    ]
    for method, url in calls:
        assert env.c.request(method, url).status_code == 401, url


# ---------------------------------------------------------------------------------------------
# import


def test_import_name_with_path_refused(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "x.pdf").write_bytes(PDF)
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        for name in ("../x.pdf", "/etc/passwd", "a/b.pdf", "..", ".", "a\\b.pdf", "x\x00.pdf", ""):
            r = env.c.post(
                f"/api/chapters/{cid}/import", json={"name": name, "page_start": 1, "page_end": 1}
            )
            assert r.status_code == 422, (name, r.text)
        assert route.call_count == 0
    assert chapter_row(env, cid).source_kind == "none"


def test_import_unknown_file_404(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    own = staged(env, pid)
    (own / "real.pdf").write_bytes(PDF)
    (own / "dir.pdf").mkdir()
    os.symlink(own / "real.pdf", own / "link.pdf")
    outside = env.imports.parent / "outside.pdf"
    outside.write_bytes(PDF)
    os.symlink(outside, own / "escape.pdf")
    (env.imports / "flat.pdf").write_bytes(PDF)  # the old flat layout is not listed
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        for name in ("nope.pdf", "dir.pdf", "link.pdf", "escape.pdf", "REAL.pdf", "flat.pdf"):
            r = env.c.post(
                f"/api/chapters/{cid}/import", json={"name": name, "page_start": 1, "page_end": 1}
            )
            assert r.status_code == 404, (name, r.text)
            assert str(env.imports) not in r.text
        assert route.call_count == 0


def test_import_is_scoped_to_the_chapters_project(env: Env) -> None:
    """A file staged for project A cannot be imported (or probed) through project B."""
    pid_a = project(env)
    (staged(env, pid_a) / "a-vol2.pdf").write_bytes(PDF)
    pid_b = project(env)  # same editor, its own approved rights record
    cid_b = chapter(env, pid_b)
    staged(env, pid_b)
    body = {"name": "a-vol2.pdf", "page_start": 1, "page_end": 1}
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        r = env.c.post(f"/api/chapters/{cid_b}/import", json=body)
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "import not found"
        # project B without an imports directory at all: the same 404
        cid_c = chapter(env, project(env))
        assert env.c.post(f"/api/chapters/{cid_c}/import", json=body).status_code == 404
        assert route.call_count == 0
    assert chapter_row(env, cid_b).source_kind == "none"


def test_import_project_dir_must_be_a_real_directory(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    elsewhere = env.imports.parent / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "v.pdf").write_bytes(PDF)
    os.symlink(elsewhere, env.imports / pid)  # a symlinked project directory is not followed
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        r = env.c.post(
            f"/api/chapters/{cid}/import", json={"name": "v.pdf", "page_start": 1, "page_end": 1}
        )
        assert r.status_code == 404, r.text
        assert route.call_count == 0


def test_bad_page_range_422(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "x.pdf").write_bytes(PDF)
    bad: list[dict[str, Any]] = [
        {"page_start": 5, "page_end": 3},  # reversed
        {"page_start": 0, "page_end": 2},  # out of bounds
        {"page_start": 1, "page_end": 1001},  # beyond the 1000-page document limit
        {"page_start": -1, "page_end": 2},
        {"page_start": "1-3", "page_end": 3},  # malformed
        {"page_start": 1.5, "page_end": 3},
        {"page_start": True, "page_end": 3},
        {"page_end": 3},
        {"page_start": 1, "page_end": 3, "extra": 1},
    ]
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        for rng in bad:
            r = env.c.post(f"/api/chapters/{cid}/import", json={"name": "x.pdf", **rng})
            assert r.status_code == 422, (rng, r.text)
        for form in (
            {"page_start": "x"},
            {"page_start": "3", "page_end": "2"},
            {"page_start": "0", "page_end": "2"},
            {"page_start": "0"},
            {"page_start": "-1", "page_end": "2"},
            {"page_start": "1", "page_end": "1001"},
        ):
            r = env.c.post(
                f"/api/chapters/{cid}/upload",
                data=form,
                files={"file": ("s.pdf", PDF, "application/pdf")},
            )
            assert r.status_code == 422, (form, r.text)
        # A single image is exactly one page, and page 0 never exists.
        r = env.c.post(
            f"/api/chapters/{cid}/upload",
            data={"page_start": "0"},
            files={"file": ("p.png", png_bytes(), "image/png")},
        )
        assert r.status_code == 422, r.text
        r = env.c.post(
            f"/api/chapters/{cid}/upload",
            data={"page_start": "1", "page_end": "2"},
            files={"file": ("p.png", png_bytes(), "image/png")},
        )
        assert r.status_code == 422, r.text
        # PDF and CBZ uploads need an explicit end page.
        r = env.c.post(
            f"/api/chapters/{cid}/upload", files={"file": ("s.pdf", PDF, "application/pdf")}
        )
        assert r.status_code == 422, r.text
        assert route.call_count == 0
    assert chapter_row(env, cid).source_kind == "none"


def test_start_ingest_called_once(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "vol1.pdf").write_bytes(PDF)
    with respx.mock(assert_all_called=True) as mock:
        route = ok_prefect(mock)
        r = env.c.post(
            f"/api/chapters/{cid}/import", json={"name": "vol1.pdf", "page_start": 2, "page_end": 9}
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert r.json() == {"job_id": job_id}
        assert route.call_count == 1
        sent = json.loads(route.calls.last.request.content)
        assert sent == {"parameters": {"chapter_id": cid}}
    row = chapter_row(env, cid)
    assert (row.source_kind, row.source_ref) == ("import", "vol1.pdf")
    assert row.source_sha256 == hashlib.sha256(PDF).hexdigest()
    assert (row.page_start, row.page_end, row.status) == (2, 9, "pending")
    with env.session() as s:
        job = s.get(JobRow, job_id)
        assert job is not None and job.flow_run_id == RUN_ID and job.chapter_id == cid
        assert job.status == "submitted"
    out = env.c.get(f"/api/chapters/{cid}").json()
    assert out["source_kind"] == "import" and "vol1.pdf" not in json.dumps(out)


def test_prefect_failure_is_502_and_chapter_stays_pending(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "v.pdf").write_bytes(PDF)
    body = {"name": "v.pdf", "page_start": 1, "page_end": 2}
    responses: list[Any] = [
        httpx.Response(500, text="Traceback: secret internals"),
        httpx.Response(201, json={"id": "not-a-uuid"}),
        httpx.ConnectError("refused"),
    ]
    for resp in responses:
        with respx.mock() as mock:
            ok_lookup(mock)
            if isinstance(resp, Exception):
                mock.post(CREATE_RUN).mock(side_effect=resp)
            else:
                mock.post(CREATE_RUN).mock(return_value=resp)
            r = env.c.post(f"/api/chapters/{cid}/import", json=body)
        assert r.status_code == 502, r.text
        assert r.json()["detail"] == "could not start ingest; try again"
        assert "secret" not in r.text and "prefect" not in r.text.lower()
        assert chapter_row(env, cid).status == "pending"
    with env.session() as s:
        jobs = s.scalars(select(JobRow).where(JobRow.chapter_id == cid)).all()
        assert {j.status for j in jobs} == {"failed"} and all(j.flow_run_id is None for j in jobs)


def test_prefect_deployment_lookup_failure_is_502(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "v.pdf").write_bytes(PDF)
    body = {"name": "v.pdf", "page_start": 1, "page_end": 2}
    for resp in (
        httpx.Response(404, json={"detail": "Deployment not found"}),
        httpx.Response(200, json={"id": "../../flow_runs"}),
        httpx.ConnectError("refused"),
    ):
        with respx.mock(assert_all_called=False) as mock:
            if isinstance(resp, Exception):
                mock.get(LOOKUP).mock(side_effect=resp)
            else:
                mock.get(LOOKUP).mock(return_value=resp)
            create = mock.post(url__regex=r".*/create_flow_run$").mock(
                return_value=httpx.Response(201, json={"id": RUN_ID})
            )
            r = env.c.post(f"/api/chapters/{cid}/import", json=body)
            assert create.call_count == 0
        assert r.status_code == 502, r.text
        assert chapter_row(env, cid).status == "pending"


# ---------------------------------------------------------------------------------------------
# upload


def test_upload_requires_editor_and_approved_rights(env: Env) -> None:
    pid = project(env, approve=False)
    cid = chapter(env, pid)
    files = {"file": ("p.png", png_bytes(), "image/png")}
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        r = env.c.post(f"/api/chapters/{cid}/upload", files=files)
        assert r.status_code == 403 and "pending" in r.json()["detail"]
        r = env.c.post(
            f"/api/chapters/{cid}/import", json={"name": "a.pdf", "page_start": 1, "page_end": 1}
        )
        assert r.status_code == 403
        login(env.c, "admin")
        assert env.c.post(f"/api/projects/{pid}/rights/approve").status_code == 200
        login(env.c, "vi")
        assert env.c.post(f"/api/chapters/{cid}/upload", files=files).status_code == 403
        login(env.c, "eve")
        r = env.c.post(f"/api/chapters/{cid}/upload", files=files)
        assert r.status_code == 404 and r.json()["detail"] == "chapter not found"
        assert route.call_count == 0
        login(env.c, "ed")
        bad_csrf = {"x-csrf-token": "nope"}
        assert (
            env.c.post(f"/api/chapters/{cid}/upload", files=files, headers=bad_csrf).status_code
            == 403
        )
        r = env.c.post(f"/api/chapters/{cid}/upload", files=files)
        assert r.status_code == 202, r.text
        assert route.call_count == 1
    row = chapter_row(env, cid)
    data = png_bytes()
    assert row.source_kind == "upload" and (row.page_start, row.page_end) == (1, 1)
    assert row.source_sha256 == hashlib.sha256(data).hexdigest()
    assert row.source_ref == f"media/sha256/{row.source_sha256}.png"
    assert env.store.get(row.source_ref) == data


def test_upload_sniffs_content_and_refuses_manhwa_pdf(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        r = env.c.post(
            f"/api/chapters/{cid}/upload",
            data={"page_start": "1", "page_end": "3"},
            files={"file": ("s.pdf", b"#!/bin/sh\necho hi\n" * 10, "application/pdf")},
        )
        assert r.status_code == 422 and r.json()["detail"] == "file type not allowed"
        r = env.c.post(f"/api/chapters/{cid}/upload", data={"page_end": "1"})
        assert r.status_code == 422
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("001.png", png_bytes())
        r = env.c.post(
            f"/api/chapters/{cid}/upload",
            data={"page_start": "1", "page_end": "1"},
            files={"file": ("c.bin", buf.getvalue(), "application/octet-stream")},
        )
        assert r.status_code == 202, r.text
        assert chapter_row(env, cid).source_ref.endswith(".cbz")

        mpid = project(env, fmt="manhwa_vertical")
        mcid = chapter(env, mpid)
        (staged(env, mpid) / "m.pdf").write_bytes(PDF)
        r = env.c.post(
            f"/api/chapters/{mcid}/upload",
            data={"page_start": "1", "page_end": "1"},
            files={"file": ("s.pdf", PDF, "application/pdf")},
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "manhwa sources must be images or CBZ"
        r = env.c.post(
            f"/api/chapters/{mcid}/import", json={"name": "m.pdf", "page_start": 1, "page_end": 1}
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "manhwa sources must be images or CBZ"
        assert route.call_count == 1


def test_upload_over_cap_is_413(tmp_path: Path) -> None:
    e = _app(tmp_path, max_upload_bytes=4096)
    pid = project(e)
    cid = chapter(e, pid)
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        # Over the request-body cap: aborted while streaming.
        big = PDF + b"0" * 200_000
        r = e.c.post(
            f"/api/chapters/{cid}/upload",
            data={"page_start": "1", "page_end": "1"},
            files={"file": ("s.pdf", big, "application/pdf")},
        )
        assert r.status_code == 413, r.text
        # Within the body slack but over the file cap: refused after parsing.
        r = e.c.post(
            f"/api/chapters/{cid}/upload",
            data={"page_start": "1", "page_end": "1"},
            files={"file": ("s.pdf", PDF + b"0" * 5000, "application/pdf")},
        )
        assert r.status_code == 413, r.text
        assert route.call_count == 0
    assert chapter_row(e, cid).source_kind == "none"


def test_refused_while_ingesting(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "v.pdf").write_bytes(PDF)
    with env.session() as s:
        row = s.get(ChapterRow, cid)
        row.status, row.ingest_started_at = "ingesting", datetime.now(UTC)
        s.commit()
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        r = env.c.post(
            f"/api/chapters/{cid}/import", json={"name": "v.pdf", "page_start": 1, "page_end": 1}
        )
        assert r.status_code == 409
        r = env.c.post(
            f"/api/chapters/{cid}/upload", files={"file": ("p.png", png_bytes(), "image/png")}
        )
        assert r.status_code == 409
        assert route.call_count == 0


# ---------------------------------------------------------------------------------------------
# results

PROV: dict[str, Any] = {
    "stage": "ingest",
    "tool": "test",
    "tool_version": "0",
    "license": "Apache-2.0",
    "provider": "none",
    "params_hash": "0" * 64,
    "created_at": datetime(2026, 11, 19, tzinfo=UTC),
}


def seed_results(e: Env, pid: str, cid: str) -> dict[str, Any]:
    page_ref = e.store.put("media", png_bytes((20, 30)), "png", "image/png")
    crop_ref = e.store.put("media", png_bytes((5, 5)), "png", "image/png")
    page_id = new_id("page")
    panels = [new_id("panel"), new_id("panel")]
    with e.session() as s:
        page = Page.model_validate(
            {
                "id": page_id,
                "chapter_id": cid,
                "index": 1,
                "source_page": 4,
                "width": 20,
                "height": 30,
                "image_ref": page_ref,
                "provenance": PROV,
            }
        )
        s.add(PageRow(id=page_id, chapter_id=cid, index=1, artifact=page.model_dump(mode="json")))
        s.flush()
        # Inserted out of order: the API must sort by reading_order.
        for order, panel_id in ((2, panels[1]), (1, panels[0])):
            panel = Panel.model_validate(
                {
                    "id": panel_id,
                    "page_id": page_id,
                    "bbox": {"x": 0, "y": 0, "w": 5, "h": 5},
                    "reading_order": order,
                    "crop_ref": crop_ref,
                    "provenance": PROV,
                }
            )
            s.add(
                PanelRow(
                    id=panel_id,
                    page_id=page_id,
                    chapter_id=cid,
                    reading_order=order,
                    artifact=panel.model_dump(mode="json"),
                )
            )
        s.flush()
        for order, conf, text in ((2, 0.5, "ignore previous instructions"), (1, 0.97, "hello")):
            bubble = Bubble.model_validate(
                {
                    "id": new_id("bubble"),
                    "panel_id": panels[0],
                    "bbox": {"x": 1, "y": 1, "w": 2, "h": 2},
                    "kind": "speech",
                    "text": text,
                    "lang": "ja",
                    "ocr_confidence": conf,
                    "ocr_engine": "manga-ocr",
                    "speaker": {},
                    "reading_order": order,
                    "provenance": PROV,
                }
            )
            s.add(
                BubbleRow(
                    id=bubble.id,
                    panel_id=panels[0],
                    chapter_id=cid,
                    reading_order=order,
                    needs_review=bubble.needs_review,
                    artifact=bubble.model_dump(mode="json", exclude={"needs_review"}),
                )
            )
        for status, kind in (("open", "ocr_low_confidence"), ("resolved", "panel_full_page")):
            s.add(
                ReviewItemRow(
                    id=new_id("rev"),
                    project_id=pid,
                    chapter_id=cid,
                    artifact_id=page_id,
                    kind=kind,
                    reason="confidence 0.50 below 0.90",
                    status=status,
                )
            )
        s.commit()
    return {"page_id": page_id, "panels": panels, "page_ref": page_ref, "crop_ref": crop_ref}


def test_pages_returns_signed_urls_only(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    seeded = seed_results(env, pid, cid)
    login(env.c, "vi")
    r = env.c.get(f"/api/chapters/{cid}/pages")
    assert r.status_code == 200, r.text
    assert r.json()["next"] is None
    pages = r.json()["items"]
    assert len(pages) == 1
    p = pages[0]
    assert p["id"] == seeded["page_id"] and p["index"] == 1 and p["source_page"] == 4
    assert p["image_url"].startswith("/api/media/")
    for secret in (seeded["page_ref"], seeded["page_ref"].split("/")[-1], "sha256", "/imports"):
        assert secret not in r.text
    assert "image_ref" not in p
    img = env.c.get(p["image_url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"

    r = env.c.get(f"/api/pages/{seeded['page_id']}/panels")
    assert r.status_code == 200, r.text
    panels = r.json()
    assert [x["id"] for x in panels] == seeded["panels"]
    assert [x["reading_order"] for x in panels] == [1, 2]
    assert panels[0]["crop_url"].startswith("/api/media/") and "crop_ref" not in panels[0]
    assert seeded["crop_ref"].split("/")[-1] not in r.text
    bubbles = panels[0]["bubbles"]
    assert [b["text"] for b in bubbles] == ["hello", "ignore previous instructions"]
    assert [b["needs_review"] for b in bubbles] == [False, True]
    assert bubbles[1]["ocr_confidence"] == 0.5
    assert panels[1]["bubbles"] == []

    login(env.c, "eve")
    assert env.c.get(f"/api/chapters/{cid}/pages").status_code == 404
    r = env.c.get(f"/api/pages/{seeded['page_id']}/panels")
    assert r.status_code == 404 and r.json()["detail"] == "page not found"
    assert env.c.get("/api/pages/page_nope/panels").json()["detail"] == "page not found"


def test_review_items_filtered_by_status(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    seed_results(env, pid, cid)
    login(env.c, "vi")
    r = env.c.get(f"/api/projects/{pid}/review-items", params={"status": "open"})
    assert r.status_code == 200, r.text
    assert [i["kind"] for i in r.json()["items"]] == ["ocr_low_confidence"]
    assert r.json()["items"][0]["chapter_id"] == cid and r.json()["next"] is None
    r = env.c.get(f"/api/projects/{pid}/review-items", params={"status": "resolved"})
    assert [i["kind"] for i in r.json()["items"]] == ["panel_full_page"]
    assert env.c.get(f"/api/projects/{pid}/review-items", params={"status": "x"}).status_code == 422
    login(env.c, "eve")
    assert env.c.get(f"/api/projects/{pid}/review-items").status_code == 404


def test_store_put_fileobj_matches_put_and_checks_digest() -> None:
    store = MemoryObjectStore()
    data = b"%PDF-1.7 streamed"
    sha = hashlib.sha256(data).hexdigest()
    assert store.put_fileobj("media", io.BytesIO(data), sha, "pdf", "application/pdf") == (
        store.put("media", data, "pdf", "application/pdf")
    )
    with pytest.raises(ValueError):
        store.put_fileobj("media", io.BytesIO(data), "0" * 64, "pdf", "application/pdf")
    with pytest.raises(ValueError):
        make_ref_from_digest("media", "../x", "pdf")


# ---------------------------------------------------------------------------------------------
# fix round 1: spool, concurrency, pagination, connection release, stale ingest


def upload_png(e: Env, cid: str) -> httpx.Response:
    return e.c.post(
        f"/api/chapters/{cid}/upload", files={"file": ("p.png", png_bytes(), "image/png")}
    )


def test_upload_slots_full_is_503(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    slots = env.c.app.state.upload_slots  # type: ignore[attr-defined]
    assert slots.try_acquire() and slots.try_acquire()  # MAX_CONCURRENT_UPLOADS = 2
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        r = upload_png(env, cid)
        assert r.status_code == 503
        assert r.json()["detail"] == "too many uploads in progress, try again"
        assert route.call_count == 0
        slots.release()
        assert upload_png(env, cid).status_code == 202
        assert route.call_count == 1
    # The slot is returned after the request, success or not.
    assert slots.try_acquire() and not slots.try_acquire()


def test_spool_oserror_is_507(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno
    import tempfile

    import starlette.formparsers

    class FullDisk(tempfile.SpooledTemporaryFile):  # type: ignore[type-arg]
        def write(self, s: Any) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    pid = project(env)
    cid = chapter(env, pid)
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", FullDisk)
    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        r = upload_png(env, cid)
        assert r.status_code == 507, r.text
        assert r.json()["detail"] == "the server could not store the upload; try again later"
        assert "No space" not in r.text
        assert route.call_count == 0
    from kagebox_api.db import AuditEvent

    with env.session() as s:
        ev = s.scalars(select(AuditEvent).where(AuditEvent.action == "upload.spool_failed")).all()
        assert [(e.target, e.detail) for e in ev] == [(cid, {"errno": errno.ENOSPC})]
    slots = env.c.app.state.upload_slots  # type: ignore[attr-defined]
    assert slots.try_acquire() and slots.try_acquire()  # released after the failure


def test_no_db_transaction_held_while_reading_source(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kagebox_api.deps import get_db
    from kagebox_api.routes import chapters as routes

    app = env.c.app
    sessions: list[Any] = []

    def spy_db() -> Iterator[Any]:
        for s in app.state.kagebox.db.session():  # type: ignore[attr-defined]
            sessions.append(s)
            yield s

    app.dependency_overrides[get_db] = spy_db  # type: ignore[attr-defined]
    seen: list[bool] = []
    real_capped, real_hash = routes._capped, routes._hash_stream

    def capped(receive: Any, cap: int) -> Any:
        inner = real_capped(receive, cap)

        async def spy() -> Any:
            seen.append(sessions[-1].in_transaction())
            return await inner()

        return spy

    def hash_stream(f: Any, cap: int | None) -> Any:
        seen.append(sessions[-1].in_transaction())
        return real_hash(f, cap)

    monkeypatch.setattr(routes, "_capped", capped)
    monkeypatch.setattr(routes, "_hash_stream", hash_stream)
    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "v.pdf").write_bytes(PDF)
    with respx.mock() as mock:
        ok_prefect(mock)
        assert upload_png(env, cid).status_code == 202
        n_upload = len(seen)
        r = env.c.post(
            f"/api/chapters/{cid}/import", json={"name": "v.pdf", "page_start": 1, "page_end": 1}
        )
        assert r.status_code == 202, r.text
    assert n_upload >= 2 and len(seen) > n_upload  # body reads + hash, then the import hash
    assert not any(seen), seen


def test_stale_ingesting_chapter_accepts_a_new_source(env: Env) -> None:
    from datetime import timedelta

    from pipeline.common.dbmodels import STALE_INGEST_S

    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "v.pdf").write_bytes(PDF)
    body = {"name": "v.pdf", "page_start": 1, "page_end": 1}

    def set_running(age_s: int) -> None:
        with env.session() as s:
            row = s.get(ChapterRow, cid)
            row.status, row.ingest_run_id = "ingesting", "old-run"
            row.ingest_started_at = datetime.now(UTC) - timedelta(seconds=age_s)
            s.commit()

    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        set_running(STALE_INGEST_S - 60)  # live run
        assert env.c.post(f"/api/chapters/{cid}/import", json=body).status_code == 409
        assert upload_png(env, cid).status_code == 409
        assert route.call_count == 0
        set_running(STALE_INGEST_S + 60)  # stale run (worker killed): may be taken over
        assert env.c.post(f"/api/chapters/{cid}/import", json=body).status_code == 202
        row = chapter_row(env, cid)
        assert (row.status, row.ingest_run_id, row.ingest_started_at) == ("pending", None, None)
        set_running(STALE_INGEST_S + 60)
        assert upload_png(env, cid).status_code == 202
        assert route.call_count == 2


def test_staleness_is_judged_on_the_heartbeat(env: Env) -> None:
    from datetime import timedelta

    from pipeline.common.dbmodels import STALE_INGEST_S

    pid = project(env)
    cid = chapter(env, pid)
    (staged(env, pid) / "v.pdf").write_bytes(PDF)
    body = {"name": "v.pdf", "page_start": 1, "page_end": 1}
    now = datetime.now(UTC)
    stale = timedelta(seconds=STALE_INGEST_S + 60)

    def set_running(started: datetime, beat: datetime | None) -> None:
        with env.session() as s:
            row = s.get(ChapterRow, cid)
            row.status, row.ingest_run_id, row.error = "ingesting", "old-run", None
            row.ingest_started_at, row.ingest_heartbeat_at = started, beat
            s.commit()

    with respx.mock(assert_all_called=False) as mock:
        route = ok_prefect(mock)
        set_running(now - timedelta(hours=3), now)  # long but alive
        assert env.c.post(f"/api/chapters/{cid}/import", json=body).status_code == 409
        out = env.c.get(f"/api/chapters/{cid}").json()
        assert (out["status"], out["error"]) == ("ingesting", None)
        set_running(now, now - stale)  # worker died: no heartbeat for > STALE
        out = env.c.get(f"/api/chapters/{cid}").json()
        assert (out["status"], out["error"]) == ("failed", "ingest was interrupted")
        assert env.c.post(f"/api/chapters/{cid}/import", json=body).status_code == 202
        row = chapter_row(env, cid)
        assert (row.status, row.ingest_run_id, row.ingest_heartbeat_at) == ("pending", None, None)
        assert route.call_count == 1


def _cursor(payload: Any) -> str:
    import base64

    raw = json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_review_items_keyset_pagination_walks_everything(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    base = datetime(2026, 11, 19, 12, tzinfo=UTC)
    seeded: set[str] = set()
    with env.session() as s:
        for i in range(11):  # several share a created_at: the id breaks the tie
            rid = new_id("rev")
            seeded.add(rid)
            s.add(
                ReviewItemRow(
                    id=rid,
                    project_id=pid,
                    chapter_id=cid,
                    artifact_id="page_x",
                    kind="page_no_text",
                    reason="r",
                    status="open",
                    created_at=base.replace(second=i // 3),
                )
            )
        s.commit()
    got: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        r = env.c.get(f"/api/projects/{pid}/review-items", params=params)
        assert r.status_code == 200, r.text
        got += [i["id"] for i in r.json()["items"]]
        cursor = r.json()["next"]
        pages += 1
        if cursor is None:
            break
    assert pages == 4 and len(got) == 11 and set(got) == seeded
    full = env.c.get(f"/api/projects/{pid}/review-items").json()
    assert [i["id"] for i in full["items"]] == got and full["next"] is None
    for bad in ("!!!", _cursor([1]), _cursor(["not-a-date", "rev_x"]), _cursor([1, "rev_x"])):
        r = env.c.get(f"/api/projects/{pid}/review-items", params={"cursor": bad})
        assert r.status_code == 422, bad
    for lim in (0, 1001):
        r = env.c.get(f"/api/projects/{pid}/review-items", params={"limit": lim})
        assert r.status_code == 422


def test_pages_keyset_pagination(env: Env) -> None:
    pid = project(env)
    cid = chapter(env, pid)
    ref = env.store.put("media", png_bytes(), "png", "image/png")
    with env.session() as s:
        for index in range(1, 6):
            page = Page.model_validate(
                {
                    "id": new_id("page"),
                    "chapter_id": cid,
                    "index": index,
                    "source_page": index,
                    "width": 8,
                    "height": 8,
                    "image_ref": ref,
                    "provenance": PROV,
                }
            )
            s.add(
                PageRow(
                    id=page.id, chapter_id=cid, index=index, artifact=page.model_dump(mode="json")
                )
            )
        s.commit()
    got: list[int] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = env.c.get(f"/api/chapters/{cid}/pages", params=params)
        assert r.status_code == 200, r.text
        got += [p["index"] for p in r.json()["items"]]
        cursor = r.json()["next"]
        if cursor is None:
            break
    assert got == [1, 2, 3, 4, 5]
    for bad in ("%%%", _cursor(["1", "page_x"]), _cursor([True, "page_x"]), _cursor([1, 2])):
        r = env.c.get(f"/api/chapters/{cid}/pages", params={"cursor": bad})
        assert r.status_code == 422, bad
