"""Jobs: the status of a background run the API started (e.g. a chapter ingest).

The ingest flow moves a job from ``submitted`` to ``running`` and then ``completed``,
``failed`` or ``cancelled``; a job whose run could not be started is ``failed`` at once.
Readable by any member of the job's project; anyone else gets the same 404 as for a missing job.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from kagebox_api.db import ChapterRow, JobRow
from kagebox_api.deps import DB, CurrentUser, require_project_role

router = APIRouter(prefix="/jobs", tags=["jobs"])

NOT_FOUND = "job not found"


class JobOut(BaseModel):
    id: str
    chapter_id: str
    status: str
    stage: str
    created_at: datetime
    updated_at: datetime


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, user: CurrentUser, db: DB) -> JobOut:
    job = db.get(JobRow, job_id)
    chapter = None if job is None else db.get(ChapterRow, job.chapter_id)
    if job is None or chapter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    try:
        require_project_role(db, user, chapter.project_id, "viewer")
    except HTTPException:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    return JobOut(
        id=job.id,
        chapter_id=job.chapter_id,
        status=job.status,
        stage=job.stage,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
