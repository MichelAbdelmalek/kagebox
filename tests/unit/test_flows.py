from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from prefect import flow
from prefect.testing.utilities import prefect_test_harness

from orchestration.flows import STAGES, chapter_pipeline, check_rights, run_chapter
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
        check_rights(lambda _: rec(), "proj_1", "understand", "cloud")
    assert check_rights(lambda _: rec(), "proj_1", "understand", "gpu")
    assert check_rights(
        lambda _: rec(allows_cloud_processing=True), "proj_1", "understand", "cloud"
    )


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
