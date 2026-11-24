"""Startup reconciliation of orphaned Prefect runs: fakes only, no live Prefect for the logic."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from prefect import flow
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from orchestration import serve
from orchestration.serve import PrefectRuns, Reconciled, RunInfo, reconcile_orphaned_runs
from pipeline.common.database import make_engine
from pipeline.common.dbmodels import (
    STALE_INGEST_S,
    Base,
    ChapterRow,
    JobRow,
    ProjectRow,
    StageRunRow,
    User,
)
from pipeline.common.settings import Settings

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(hours=2)  # long past the stale window
FRESH_BEAT = NOW - timedelta(seconds=30)
RUN_A, RUN_B = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
CHAPTER = "chap_1"


@dataclass
class FakeRuns:
    by_deployment: dict[str, list[RunInfo]]
    crashed: list[tuple[str, str]] = field(default_factory=list)
    fail_crash: bool = False

    def running(self, deployment: str) -> list[RunInfo]:
        return list(self.by_deployment.get(deployment, []))

    def crash(self, run_id: str, message: str) -> None:
        if self.fail_crash:
            raise ConnectionError("prefect is down")
        self.crashed.append((run_id, message))


@pytest.fixture(scope="module", autouse=True)
def harness() -> Iterator[None]:
    with prefect_test_harness(server_startup_timeout=120):
        yield


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_engine(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))
    Base.metadata.create_all(eng)
    with Session(eng) as s, s.begin():
        s.add(User(id="user_1", email="a@b.c", password_hash="x"))
        s.flush()
        s.add(ProjectRow(id="proj_1", title="T", format="manga_rtl", created_by="user_1"))
        s.flush()
        s.add(
            ChapterRow(
                id=CHAPTER,
                project_id="proj_1",
                number=1,
                language="ja",
                format="manga_rtl",
                source_kind="import",
                source_ref="x.cbz",
                source_sha256="b" * 64,
                page_start=1,
                page_end=1,
                status="ready",
            )
        )
    return eng


def set_stage(eng: Engine, run_id: str, beat: datetime) -> None:
    with Session(eng) as s, s.begin():
        s.add(
            StageRunRow(
                chapter_id=CHAPTER,
                stage="understand",
                status="running",
                run_id=run_id,
                started_at=OLD,
                heartbeat_at=beat,
            )
        )


def set_ingesting(eng: Engine, run_id: str, beat: datetime) -> None:
    with Session(eng) as s, s.begin():
        row = s.get(ChapterRow, CHAPTER)
        assert row is not None
        row.status, row.ingest_run_id = "ingesting", run_id
        row.ingest_started_at = row.ingest_heartbeat_at = beat


def add_job(eng: Engine, run_id: str, stage: str) -> None:
    with Session(eng) as s, s.begin():
        s.add(
            JobRow(
                id=f"job_{stage}_{run_id[:4]}",
                chapter_id=CHAPTER,
                flow_run_id=run_id,
                status="running",
                stage=stage,
            )
        )


def job_status(eng: Engine, stage: str, run_id: str) -> str:
    with Session(eng) as s:
        job = s.get(JobRow, f"job_{stage}_{run_id[:4]}")
        assert job is not None
        return job.status


def stage_state(eng: Engine) -> tuple[str, str | None]:
    with Session(eng) as s:
        row = s.get(StageRunRow, (CHAPTER, "understand"))
        assert row is not None
        return row.status, row.error


def old_run(run_id: str = RUN_A, chapter: str | None = CHAPTER) -> RunInfo:
    return RunInfo(run_id, chapter, OLD)


def test_dead_understand_run_is_crashed_and_its_rows_failed(engine: Engine) -> None:
    set_stage(engine, RUN_A, OLD)  # no heartbeat for hours: the worker died
    add_job(engine, RUN_A, "understand")
    runs = FakeRuns({"chapter-understand": [old_run()]})
    assert reconcile_orphaned_runs(engine, runs, NOW) == Reconciled(crashed=1, kept=0, errors=0)
    assert [r for r, _ in runs.crashed] == [RUN_A]
    assert job_status(engine, "understand", RUN_A) == "failed"
    assert stage_state(engine) == ("failed", "understand was interrupted")


def test_alive_understand_run_is_never_touched(engine: Engine) -> None:
    set_stage(engine, RUN_A, FRESH_BEAT)
    add_job(engine, RUN_A, "understand")
    runs = FakeRuns({"chapter-understand": [old_run()]})
    assert reconcile_orphaned_runs(engine, runs, NOW) == Reconciled(crashed=0, kept=1, errors=0)
    assert runs.crashed == []
    assert job_status(engine, "understand", RUN_A) == "running"
    assert stage_state(engine)[0] == "running"


def test_stage_owned_by_another_run_does_not_keep_this_run_alive(engine: Engine) -> None:
    set_stage(engine, RUN_B, FRESH_BEAT)  # RUN_B is alive; RUN_A is the orphan holding the slot
    runs = FakeRuns({"chapter-understand": [old_run(RUN_A)]})
    assert reconcile_orphaned_runs(engine, runs, NOW).crashed == 1
    assert stage_state(engine)[0] == "running"  # RUN_B's row is not RUN_A's to fail


def test_ingest_run_dead_and_alive(engine: Engine) -> None:
    set_ingesting(engine, RUN_A, OLD)
    add_job(engine, RUN_A, "ingest")
    runs = FakeRuns({"chapter-ingest": [old_run()]})
    assert reconcile_orphaned_runs(engine, runs, NOW).crashed == 1
    assert job_status(engine, "ingest", RUN_A) == "failed"
    with Session(engine) as s:
        row = s.get(ChapterRow, CHAPTER)
        assert row is not None and (row.status, row.error) == ("failed", "ingest was interrupted")
    set_ingesting(engine, RUN_B, FRESH_BEAT)
    runs = FakeRuns({"chapter-ingest": [old_run(RUN_B)]})
    assert reconcile_orphaned_runs(engine, runs, NOW) == Reconciled(0, 1, 0)
    assert runs.crashed == []


def test_pipeline_run_is_kept_while_any_stage_of_its_chapter_is_alive(engine: Engine) -> None:
    runs = FakeRuns({"chapter-pipeline": [old_run(RUN_A)]})
    set_stage(engine, RUN_B, FRESH_BEAT)  # the sub-flow run has its own id
    assert reconcile_orphaned_runs(engine, runs, NOW) == Reconciled(0, 1, 0)
    with Session(engine) as s, s.begin():
        row = s.get(StageRunRow, (CHAPTER, "understand"))
        assert row is not None
        row.heartbeat_at = OLD
    assert reconcile_orphaned_runs(engine, runs, NOW).crashed == 1
    assert [r for r, _ in runs.crashed] == [RUN_A]


def test_young_runs_and_runs_without_a_chapter_are_left_alone(engine: Engine) -> None:
    young = RunInfo(RUN_A, CHAPTER, NOW - timedelta(seconds=10))  # may not have claimed yet
    no_chapter = old_run(RUN_B, chapter=None)
    unknown_start = RunInfo("33333333-3333-3333-3333-333333333333", CHAPTER, None)
    runs = FakeRuns({"chapter-understand": [young, no_chapter, unknown_start]})
    assert reconcile_orphaned_runs(engine, runs, NOW) == Reconciled(0, 3, 0)
    assert runs.crashed == []


def test_a_failed_crash_call_is_counted_and_leaves_the_rows_alone(engine: Engine) -> None:
    set_stage(engine, RUN_A, OLD)
    add_job(engine, RUN_A, "understand")
    runs = FakeRuns({"chapter-understand": [old_run()]}, fail_crash=True)
    assert reconcile_orphaned_runs(engine, runs, NOW) == Reconciled(0, 0, 1)
    assert job_status(engine, "understand", RUN_A) == "running"


def test_only_counts_are_logged(engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    set_stage(engine, RUN_A, OLD)
    runs = FakeRuns({"chapter-understand": [old_run()], "chapter-ingest": [old_run(RUN_B)]})
    reconcile_orphaned_runs(engine, runs, NOW)
    assert "2 orphaned runs marked crashed" in caplog.text
    for secret in (RUN_A, RUN_B, CHAPTER, "proj_1"):
        assert secret not in caplog.text


def test_stale_window_is_the_shared_constant(engine: Engine) -> None:
    edge = NOW - timedelta(seconds=STALE_INGEST_S - 5)  # heartbeat just inside the window
    set_stage(engine, RUN_A, edge)
    runs = FakeRuns({"chapter-understand": [old_run()]})
    assert reconcile_orphaned_runs(engine, runs, NOW).kept == 1


def test_startup_never_blocks_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_db() -> Engine:
        raise OSError("no database")

    serve._reconcile_at_start(build_engine=no_db)  # does not raise

    class Boom:
        def running(self, deployment: str) -> list[RunInfo]:
            raise ConnectionError("prefect down")

        def crash(self, run_id: str, message: str) -> None:  # pragma: no cover - unused
            raise AssertionError

    eng = make_engine(Settings(database_url="sqlite://"))
    serve._reconcile_at_start(build_engine=lambda: eng, runs=Boom())  # does not raise


def test_main_reconciles_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(serve, "_reconcile_at_start", lambda: order.append("reconcile"))
    monkeypatch.setattr(serve, "serve", lambda *a, **k: order.append("serve"))
    serve.main()
    assert order == ["reconcile", "serve"]


def test_every_served_deployment_is_reconciled() -> None:
    assert set(serve.KINDS) == {"chapter-pipeline", "chapter-ingest", "chapter-understand"}


# One real round trip against Prefect's temporary test server: the filters and the state call are
# the only parts the fakes cannot cover.
@flow
def _stuck() -> None:  # pragma: no cover - never run
    return None


def test_prefect_client_reads_running_runs_and_sets_crashed() -> None:
    import asyncio

    from prefect.client.orchestration import get_client
    from prefect.client.schemas.objects import State, StateType

    assert PrefectRuns().running("chapter-understand") == []  # filters accepted

    async def make() -> str:
        async with get_client() as c:
            created = await c.create_flow_run(_stuck)
            await c.set_flow_run_state(created.id, State(type=StateType.RUNNING), force=True)
            return str(created.id)

    run_id = asyncio.run(make())
    PrefectRuns().crash(run_id, "test")

    async def state() -> str:
        async with get_client() as c:
            got = await c.read_flow_run(run_id)
            assert got.state is not None
            return str(got.state.type)

    assert "CRASHED" in asyncio.run(state())
