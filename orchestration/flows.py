"""Prefect flows: the chapter pipeline with the rights gate in front of every stage.

Every stage re-checks rights itself, so a record revoked mid-run stops the next stage.
Stages with a runner (ingest: ``orchestration.ingest_flow``, understand:
``orchestration.understand_flow``) do real work; the others report ``not_implemented`` until their
Phase 1 milestone lands.
"""

# No `from __future__ import annotations`: Prefect builds a Pydantic model from the flow
# signature at run time and needs real (not string) annotations.

import json
from collections.abc import Callable, Mapping
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
# Stages that send page content to a hosted provider in the cloud profile. Understand runs on a
# local model in every profile, so it needs the render right only.
CLOUD_STAGES: frozenset[Stage] = frozenset({"director"})

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
StageRunner = Callable[[str], StageResult]  # chapter_id -> result


@task(retries=0)
def run_stage(
    load: RightsLoader, project_id: str, chapter_id: str, stage: Stage, profile: str
) -> StageResult:
    check_rights(load, project_id, stage, profile)
    return {"stage": stage, "chapter_id": chapter_id, "status": "not_implemented"}


Profile = Literal["cloud", "gpu"]

UNDERSTAND_SKIPPED = "skipped: trial and confirmation required"


def understand_runner(full: StageRunner, confirm_full: bool) -> StageRunner:
    """A full understand run takes hours: the pipeline starts one only when it was confirmed
    (after a trial); otherwise the stage is skipped without running a model."""
    if confirm_full:
        return full

    def skipped(chapter_id: str) -> StageResult:
        return {"stage": "understand", "chapter_id": chapter_id, "status": UNDERSTAND_SKIPPED}

    return skipped


def run_chapter(
    load: RightsLoader,
    project_id: str,
    chapter_id: str,
    profile: Profile,
    runners: Mapping[Stage, StageRunner] | None = None,
) -> list[StageResult]:
    """The pipeline body, with the rights source and stage runners injected."""
    check_rights(load, project_id, "ingest", profile)  # fail fast before any work
    out: list[StageResult] = []
    for stage in STAGES:
        runner = (runners or {}).get(stage)
        if runner is None:
            out.append(run_stage(load, project_id, chapter_id, stage, profile))
        else:
            check_rights(load, project_id, stage, profile)
            out.append(runner(chapter_id))
    return out


@flow(name="chapter-pipeline", log_prints=False, validate_parameters=True)
def chapter_pipeline(
    project_id: str,
    chapter_id: str,
    profile: Profile = "cloud",
    confirm_full_understand: bool = False,
) -> list[StageResult]:
    """Deployment entry point. Parameters must stay JSON-serializable."""
    from orchestration.ingest_flow import ingest_stage  # deferred: they import this module
    from orchestration.understand_flow import understand_stage
    from pipeline.common.database import make_engine  # deferred: needs settings at run time
    from pipeline.common.settings import get_settings

    return run_chapter(
        db_rights_loader(make_engine(get_settings())),
        project_id,
        chapter_id,
        profile,
        runners={
            "ingest": ingest_stage,
            "understand": understand_runner(understand_stage, confirm_full_understand),
        },
    )
