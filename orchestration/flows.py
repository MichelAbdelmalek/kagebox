"""Prefect flows. Phase 0: the chapter pipeline skeleton with the rights gate in front.

Every stage re-checks rights itself, so a record revoked mid-run stops the next stage.
Stage bodies arrive in Phase 1; until then each stage reports ``not_implemented``.
"""

# No `from __future__ import annotations`: Prefect builds a Pydantic model from the flow
# signature at run time and needs real (not string) annotations.

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from prefect import flow, task
from sqlalchemy import Engine, text

from pipeline.common.schemas import RightsRecord, require_cloud_allowed, require_render_allowed
from pipeline.common.schemas.base import Stage

STAGES: tuple[Stage, ...] = (
    "ingest",
    "understand",
    "bible",
    "director",
    "render",
    "audio",
    "validate",
    "assemble",
)
# Stages that send page content to a hosted provider in the cloud profile.
CLOUD_STAGES: frozenset[Stage] = frozenset({"understand", "director"})

RightsLoader = Callable[[str], RightsRecord | None]


def db_rights_loader(engine: Engine) -> RightsLoader:
    def load(project_id: str) -> RightsRecord | None:
        with engine.connect() as conn:
            raw = conn.execute(
                text("SELECT record FROM rights_records WHERE project_id = :p"), {"p": project_id}
            ).scalar_one_or_none()
        if raw is None:
            return None
        return RightsRecord.model_validate_json(raw if isinstance(raw, str) else json.dumps(raw))

    return load


def check_rights(load: RightsLoader, project_id: str, stage: Stage, profile: str) -> RightsRecord:
    now = datetime.now(UTC)
    rec = load(project_id)
    if profile == "cloud" and stage in CLOUD_STAGES:
        return require_cloud_allowed(rec, now)
    return require_render_allowed(rec, now)


StageResult = dict[str, str]


@task(retries=0)
def run_stage(
    load: RightsLoader, project_id: str, chapter_id: str, stage: Stage, profile: str
) -> StageResult:
    check_rights(load, project_id, stage, profile)
    return {"stage": stage, "chapter_id": chapter_id, "status": "not_implemented"}


Profile = Literal["cloud", "gpu"]


def run_chapter(
    load: RightsLoader, project_id: str, chapter_id: str, profile: Profile
) -> list[StageResult]:
    """The pipeline body, with the rights source injected (tests pass a fake loader)."""
    check_rights(load, project_id, "ingest", profile)  # fail fast before any work
    return [run_stage(load, project_id, chapter_id, s, profile) for s in STAGES]


@flow(name="chapter-pipeline", log_prints=False, validate_parameters=True)
def chapter_pipeline(
    project_id: str, chapter_id: str, profile: Profile = "cloud"
) -> list[StageResult]:
    """Deployment entry point. Parameters must stay JSON-serializable."""
    from pipeline.common.database import make_engine  # deferred: needs settings at run time
    from pipeline.common.settings import get_settings

    return run_chapter(
        db_rights_loader(make_engine(get_settings())), project_id, chapter_id, profile
    )
