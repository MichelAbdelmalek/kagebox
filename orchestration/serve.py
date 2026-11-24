"""Worker entry point: serve the chapter flows to the Prefect server on the internal network.

Deployments: ``chapter-pipeline/chapter-pipeline``, ``chapter-ingest/chapter-ingest`` and
``chapter-understand/chapter-understand`` (the API starts the latter two through Prefect's REST
API). One heavy flow at a time (``limit=1``): ingest and the local vision model are CPU- and
RAM-heavy.

Startup reconciliation: after a Docker or worker crash a flow run can stay ``Running`` in Prefect
and hold its deployment's only concurrency slot, so every later start waits in
``AwaitingConcurrencySlot`` forever. Before serving, :func:`reconcile_orphaned_runs` sets such runs
``Crashed`` and fails their job rows. A run is left alone whenever the database shows it alive
(its stage or chapter row is owned by it with a heartbeat younger than ``STALE_INGEST_S``) or it
started less than ``YOUNG_RUN_S`` ago (it may not have claimed its row yet). Only counts are logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import UUID

from prefect import serve
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import (
    DeploymentFilter,
    DeploymentFilterName,
    FlowRunFilter,
    FlowRunFilterState,
    FlowRunFilterStateType,
)
from prefect.client.schemas.objects import State, StateType
from prefect.client.schemas.sorting import FlowRunSort
from prefect.deployments.runner import RunnerDeployment
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from orchestration import ingest_flow, understand_flow
from orchestration.flows import chapter_pipeline
from orchestration.ingest_flow import FLOW_NAME
from orchestration.ingest_flow import ingest_flow as ingest_flow_fn
from orchestration.understand_flow import FLOW_NAME as UNDERSTAND_FLOW_NAME
from orchestration.understand_flow import understand_flow as understand_flow_fn
from pipeline.common.dbmodels import (
    ChapterRow,
    StageRunRow,
    aware,
    ingest_alive,
    ingest_stale_before,
    stage_alive,
)

log = logging.getLogger(__name__)

PIPELINE_DEPLOYMENT = "chapter-pipeline"
YOUNG_RUN_S = 120  # a run this young may not have claimed its row yet: never reconciled
ORPHANED = "the worker that ran this flow stopped; marked crashed at worker start"

Kind = Literal["pipeline", "ingest", "understand"]
KINDS: dict[str, Kind] = {
    PIPELINE_DEPLOYMENT: "pipeline",
    FLOW_NAME: "ingest",
    UNDERSTAND_FLOW_NAME: "understand",
}


@dataclass(frozen=True)
class RunInfo:
    id: str
    chapter_id: str | None
    started: datetime | None


class RunSource(Protocol):
    """The two Prefect operations reconciliation needs (a fake in tests)."""

    def running(self, deployment: str) -> list[RunInfo]: ...

    def crash(self, run_id: str, message: str) -> None: ...


class PrefectRuns:
    """``RunSource`` on the Prefect REST API (the worker's own client settings)."""

    def running(self, deployment: str) -> list[RunInfo]:
        async def read() -> list[RunInfo]:
            async with get_client() as client:
                runs = await client.read_flow_runs(
                    deployment_filter=DeploymentFilter(
                        name=DeploymentFilterName(any_=[deployment])
                    ),
                    flow_run_filter=FlowRunFilter(
                        state=FlowRunFilterState(
                            type=FlowRunFilterStateType(any_=[StateType.RUNNING])
                        )
                    ),
                    sort=FlowRunSort.START_TIME_ASC,  # this client sends null sort/limit otherwise,
                    limit=200,  # which the server rejects; a worker never has more running runs
                )
            out: list[RunInfo] = []
            for r in runs:
                chapter = r.parameters.get("chapter_id")
                started = r.start_time or r.expected_start_time
                out.append(
                    RunInfo(str(r.id), chapter if isinstance(chapter, str) else None, started)
                )
            return out

        return asyncio.run(read())

    def crash(self, run_id: str, message: str) -> None:
        async def go() -> None:
            async with get_client() as client:
                await client.set_flow_run_state(
                    UUID(run_id), State(type=StateType.CRASHED, message=message), force=True
                )

        asyncio.run(go())


@dataclass(frozen=True)
class Reconciled:
    crashed: int = 0
    kept: int = 0
    errors: int = 0


def _alive(engine: Engine, kind: Kind, run: RunInfo, now: datetime) -> bool:
    """True when the database shows this run (or, for the pipeline, its chapter) alive."""
    if run.chapter_id is None:
        return True  # not judgeable: leave it alone
    stale_before = ingest_stale_before(now)
    with Session(engine) as s:
        chapter = s.get(ChapterRow, run.chapter_id)
        stage = s.get(StageRunRow, (run.chapter_id, "understand"))
        if kind == "ingest":
            return bool(
                chapter and chapter.ingest_run_id == run.id and ingest_alive(chapter, stale_before)
            )
        if kind == "understand":
            return bool(stage and stage.run_id == run.id and stage_alive(stage, stale_before))
        # pipeline: its stages run as sub-flow runs with their own ids, so any live row counts
        return bool(
            (chapter and ingest_alive(chapter, stale_before))
            or (stage and stage_alive(stage, stale_before))
        )


def _mark_rows(engine: Engine, kind: Kind, run: RunInfo) -> None:
    """Fail the job, and the stage or chapter row if this run still owns it (owner-only)."""
    ingest_flow._set_job(engine, run.id, "failed")
    if run.chapter_id is None:
        return
    if kind == "ingest":
        ingest_flow._fail(engine, run.chapter_id, ingest_flow.INTERRUPTED, run.id)
    elif kind == "understand":
        understand_flow._fail(engine, run.chapter_id, understand_flow.INTERRUPTED, run.id)


def reconcile_orphaned_runs(
    engine: Engine,
    runs: RunSource,
    now: datetime | None = None,
    deployments: dict[str, Kind] | None = None,
) -> Reconciled:
    """Crash ``Running`` flow runs of our deployments whose rows are not alive.

    Never raises for one run's failure (counted in ``errors``); logs counts only.
    """
    now = now or datetime.now(UTC)
    crashed = kept = errors = 0
    for deployment, kind in (deployments or KINDS).items():
        for run in runs.running(deployment):
            young = run.started is None or aware(run.started) > now - timedelta(seconds=YOUNG_RUN_S)
            if young or _alive(engine, kind, run, now):
                kept += 1
                continue
            try:
                runs.crash(run.id, ORPHANED)
            except Exception as exc:
                log.warning("could not mark an orphaned run crashed: %s", type(exc).__name__)
                errors += 1
                continue
            crashed += 1
            try:
                _mark_rows(engine, kind, run)
            except Exception as exc:  # the run is crashed; the UI catches up on the stale window
                log.warning("could not update rows of a crashed run: %s", type(exc).__name__)
                errors += 1
    result = Reconciled(crashed, kept, errors)
    log.info(
        "reconcile: %d orphaned runs marked crashed, %d left alone, %d errors",
        result.crashed,
        result.kept,
        result.errors,
    )
    return result


def _reconcile_at_start(
    build_engine: Callable[[], Engine] = ingest_flow.build_engine,
    runs: RunSource | None = None,
) -> None:
    """Best effort: a failure here must not keep the worker from serving."""
    try:
        engine = build_engine()
    except Exception as exc:
        log.warning("reconcile skipped, no database: %s", type(exc).__name__)
        return
    try:
        reconcile_orphaned_runs(engine, runs or PrefectRuns())
    except Exception as exc:
        log.warning("reconcile failed: %s", type(exc).__name__)
    finally:
        engine.dispose()


def main() -> None:
    _reconcile_at_start()
    # to_deployment is sync here (no running event loop); its stub also allows a coroutine.
    pipeline = cast(RunnerDeployment, chapter_pipeline.to_deployment(name="chapter-pipeline"))
    ingest = cast(
        RunnerDeployment, ingest_flow_fn.to_deployment(name=FLOW_NAME, concurrency_limit=1)
    )
    understand = cast(
        RunnerDeployment,
        understand_flow_fn.to_deployment(name=UNDERSTAND_FLOW_NAME, concurrency_limit=1),
    )
    serve(pipeline, ingest, understand, limit=1)


if __name__ == "__main__":
    main()
