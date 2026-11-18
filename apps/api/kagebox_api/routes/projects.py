from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from kagebox_api.db import ProjectMember, ProjectRow, RightsRow, audit, aware, utcnow
from kagebox_api.deps import DB, CurrentUser, require_project_role, state
from kagebox_api.security import sign_media
from pipeline.common.ids import new_id
from pipeline.common.schemas import RightsDenied, RightsRecord, require_render_allowed
from pipeline.common.uploads import DOC_MIMES, UploadRejected, reencode_image, sniff

router = APIRouter(prefix="/projects", tags=["projects"])

Format = Literal["manga_rtl", "comic_ltr", "manhwa_vertical"]


class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    format: Format


class RightsOut(BaseModel):
    id: str
    basis: str
    status: str
    justification: str
    licensor: str | None
    allows_cloud_processing: bool
    expires_at: datetime | None
    evidence_url: str | None


class ProjectOut(BaseModel):
    id: str
    title: str
    format: str
    role: str
    rights: RightsOut | None


def _rights(row: RightsRow | None) -> RightsRecord | None:
    return None if row is None else RightsRecord.model_validate_json(_json(row.record))


def _json(d: dict[str, object]) -> str:
    import json

    return json.dumps(d)


def _rights_out(request: Request, user_id: str, rec: RightsRecord | None) -> RightsOut | None:
    return None if rec is None else _record_out(request, user_id, rec)


def _record_out(request: Request, user_id: str, rec: RightsRecord) -> RightsOut:
    st = state(request)
    url = None
    if rec.evidence_doc_ref:
        tok = sign_media(
            st.media_key, rec.evidence_doc_ref, user_id, st.settings.media_url_ttl_seconds
        )
        url = f"/api/media/{tok}"
    return RightsOut(
        id=rec.id,
        basis=rec.basis,
        status=rec.status,
        justification=rec.justification,
        licensor=rec.licensor,
        allows_cloud_processing=rec.allows_cloud_processing,
        expires_at=rec.expires_at,
        evidence_url=url,
    )


def _out(request: Request, db: DB, user_id: str, p: ProjectRow, role: str) -> ProjectOut:
    row = db.scalar(select(RightsRow).where(RightsRow.project_id == p.id))
    return ProjectOut(
        id=p.id,
        title=p.title,
        format=p.format,
        role=role,
        rights=_rights_out(request, user_id, _rights(row)),
    )


@router.get("", response_model=list[ProjectOut])
def list_projects(request: Request, user: CurrentUser, db: DB) -> list[ProjectOut]:
    if user.role == "admin":
        rows = [
            (p, "owner") for p in db.scalars(select(ProjectRow).order_by(ProjectRow.created_at))
        ]
    else:
        q = (
            select(ProjectRow, ProjectMember.role)
            .join(ProjectMember, ProjectMember.project_id == ProjectRow.id)
            .where(ProjectMember.user_id == user.id)
            .order_by(ProjectRow.created_at)
        )
        rows = [(p, r) for p, r in db.execute(q).all()]
    return [_out(request, db, user.id, p, r) for p, r in rows]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectIn, request: Request, user: CurrentUser, db: DB) -> ProjectOut:
    p = ProjectRow(
        id=new_id("proj"), title=body.title.strip(), format=body.format, created_by=user.id
    )
    db.add(p)
    db.flush()  # the membership row references the project
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    audit(db, user.id, "project.create", p.id)
    db.commit()
    return _out(request, db, user.id, p, "owner")


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, request: Request, user: CurrentUser, db: DB) -> ProjectOut:
    role = require_project_role(db, user, project_id, "viewer")
    p = db.get(ProjectRow, project_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return _out(request, db, user.id, p, role)


@router.post("/{project_id}/rights", response_model=RightsOut, status_code=201)
async def submit_rights(
    project_id: str,
    request: Request,
    user: CurrentUser,
    db: DB,
    evidence: UploadFile,
    basis: Annotated[Literal["owned", "licensed", "public_domain"], Form()],
    justification: Annotated[str, Form(min_length=1, max_length=500)],
    licensor: Annotated[str | None, Form(max_length=500)] = None,
    territory: Annotated[str | None, Form(max_length=500)] = None,
    allows_cloud_processing: Annotated[bool, Form()] = False,
    expires_at: Annotated[datetime | None, Form()] = None,
) -> RightsOut:
    """Submit (or resubmit) the rights record. It always starts as ``pending``."""
    require_project_role(db, user, project_id, "editor")
    st = state(request)
    data = await evidence.read(st.settings.max_upload_bytes + 1)
    try:
        kind = sniff(data, DOC_MIMES, st.settings.max_upload_bytes)
        if kind.mime.startswith("image/"):
            data, ext, mime = reencode_image(data), "png", "image/png"
        else:
            ext, mime = kind.ext, kind.mime
    except UploadRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    ref = st.store.put("evidence", data, ext, mime)
    existing = db.scalar(select(RightsRow).where(RightsRow.project_id == project_id))
    try:
        rec = RightsRecord(
            id=existing.id if existing else new_id("rights"),
            project_id=project_id,
            basis=basis,
            evidence_doc_ref=ref,
            justification=justification.strip(),
            licensor=licensor or None,
            territory=territory or None,
            allows_cloud_processing=allows_cloud_processing,
            expires_at=aware(expires_at) if expires_at else None,
            created_by=user.id,
        )
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc
    payload = rec.model_dump(mode="json")
    if existing:
        existing.record, existing.status = payload, rec.status
    else:
        db.add(RightsRow(id=rec.id, project_id=project_id, status=rec.status, record=payload))
    audit(db, user.id, "rights.submit", project_id, basis=basis, evidence=ref)
    db.commit()
    return _record_out(request, user.id, rec)


def _decide(
    project_id: str,
    request: Request,
    user: CurrentUser,
    db: DB,
    decision: Literal["approved", "rejected"],
) -> RightsOut:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only an admin can decide rights")
    row = db.scalar(select(RightsRow).where(RightsRow.project_id == project_id))
    rec = _rights(row)
    if row is None or rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no rights record")
    try:
        new = RightsRecord.model_validate_json(
            rec.model_copy(
                update={
                    "status": decision,
                    "approved_by": user.id if decision == "approved" else None,
                    "approved_at": utcnow() if decision == "approved" else None,
                }
            ).model_dump_json()
        )
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, exc.errors(include_url=False, include_context=False)
        ) from exc
    row.record, row.status = new.model_dump(mode="json"), new.status
    audit(db, user.id, f"rights.{decision}", project_id)
    db.commit()
    return _record_out(request, user.id, new)


@router.post("/{project_id}/rights/approve", response_model=RightsOut)
def approve_rights(project_id: str, request: Request, user: CurrentUser, db: DB) -> RightsOut:
    return _decide(project_id, request, user, db, "approved")


@router.post("/{project_id}/rights/reject", response_model=RightsOut)
def reject_rights(project_id: str, request: Request, user: CurrentUser, db: DB) -> RightsOut:
    return _decide(project_id, request, user, db, "rejected")


class RenderAccepted(BaseModel):
    status: Literal["accepted"]
    detail: str


@router.post("/{project_id}/render", response_model=RenderAccepted, status_code=202)
def request_render(project_id: str, user: CurrentUser, db: DB) -> RenderAccepted:
    """Server-side rights gate. Phase 1 wires this to the Prefect chapter pipeline."""
    require_project_role(db, user, project_id, "editor")
    row = db.scalar(select(RightsRow).where(RightsRow.project_id == project_id))
    try:
        require_render_allowed(_rights(row), utcnow())
    except RightsDenied as exc:
        audit(db, user.id, "render.denied", project_id, reason=str(exc))
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    audit(db, user.id, "render.accepted", project_id)
    db.commit()
    return RenderAccepted(status="accepted", detail="pipeline not implemented until Phase 1")
