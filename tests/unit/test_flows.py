from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from prefect import flow
from prefect.testing.utilities import prefect_test_harness

from orchestration.flows import (
    CLOUD_STAGES,
    STAGES,
    chapter_pipeline,
    check_rights,
    run_chapter,
)
from pipeline.common.schemas import RightsDenied, RightsRecord

REF = "evidence/sha256/" + "a" * 64 + ".pdf"


def rec(**over: Any) -> RightsRecord:
    base: dict[str, Any] = {
        "id": "rights_1",
        "project_id": "proj_1",
        "basis": "public_domain",
        "justification": "PD",
        "created_by": "user_a",
        "status": "approved",
        "evidence_doc_ref": REF,
        "approved_by": "user_b",
        "approved_at": datetime.now(UTC),
    }
    base.update(over)
    return RightsRecord(**base)


@flow
def harness_flow(load: Any, profile: str = "cloud") -> list[dict[str, str]]:
    return run_chapter(load, "proj_1", "chap_1", profile)  # type: ignore[arg-type]


@pytest.fixture(scope="module", autouse=True)
def harness() -> Iterator[None]:
    with prefect_test_harness(server_startup_timeout=120):
        yield


def test_pipeline_refuses_without_approved_rights() -> None:
    with pytest.raises(RightsDenied):
        harness_flow(load=lambda _: None)
    with pytest.raises(RightsDenied):
        harness_flow(
            load=lambda _: rec(status="pending", approved_by=None, approved_at=None),
        )


def test_cloud_stages_need_cloud_permission() -> None:
    with pytest.raises(RightsDenied, match="hosted provider"):
        check_rights(lambda _: rec(), "proj_1", "director", "cloud")
    assert check_rights(lambda _: rec(), "proj_1", "director", "gpu")
    assert check_rights(lambda _: rec(allows_cloud_processing=True), "proj_1", "director", "cloud")


def test_understand_stage_needs_render_rights_only() -> None:
    """Understand runs a local model in every profile: the cloud-processing gate is not its."""
    assert "understand" not in CLOUD_STAGES
    no_cloud = rec(allows_cloud_processing=False)
    assert check_rights(lambda _: no_cloud, "proj_1", "understand", "cloud")
    with pytest.raises(RightsDenied):  # but the render right is still required
        check_rights(
            lambda _: rec(status="pending", approved_by=None, approved_at=None),
            "proj_1",
            "understand",
            "cloud",
        )
    ran: list[str] = []

    def runner(chapter_id: str) -> dict[str, str]:
        ran.append(chapter_id)
        return {"stage": "understand", "chapter_id": chapter_id, "status": "done"}

    with pytest.raises(RightsDenied, match="hosted provider"):  # director, a later stage
        run_chapter(lambda _: no_cloud, "proj_1", "chap_1", "cloud", {"understand": runner})
    assert ran == ["chap_1"]  # the stage itself ran


def test_chapter_pipeline_wires_the_understand_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestration.flows as flows_mod
    import orchestration.understand_flow as understand_mod
    import pipeline.common.database as database_mod
    import pipeline.common.settings as settings_mod

    seen: dict[str, Any] = {}

    def fake_run_chapter(
        load: Any, project_id: str, chapter_id: str, profile: str, runners: Any
    ) -> list[dict[str, str]]:
        seen["runners"] = runners
        return []

    monkeypatch.setattr(flows_mod, "run_chapter", fake_run_chapter)
    monkeypatch.setattr(database_mod, "make_engine", lambda s: object())
    monkeypatch.setattr(settings_mod, "get_settings", lambda: object())
    monkeypatch.setattr(flows_mod, "db_rights_loader", lambda e: lambda p: None)
    chapter_pipeline("proj_1", "chap_1")
    assert seen["runners"]["understand"] is understand_mod.understand_stage


def test_pipeline_runs_all_stages_when_allowed() -> None:
    out = harness_flow(load=lambda _: rec(allows_cloud_processing=True))
    assert [r["stage"] for r in out] == list(STAGES)


def test_deployment_parameters_validate() -> None:
    """A deployment run validates parameters from JSON; this broke once in the live stack."""
    params = chapter_pipeline.validate_parameters({"project_id": "proj_1", "chapter_id": "c"})
    assert params["profile"] == "cloud"
    with pytest.raises(Exception, match="profile"):
        chapter_pipeline.validate_parameters(
            {"project_id": "proj_1", "chapter_id": "c", "profile": "nope"}
        )
