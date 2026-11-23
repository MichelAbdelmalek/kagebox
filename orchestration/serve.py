"""Worker entry point: serve the chapter flows to the Prefect server on the internal network.

Deployments: ``chapter-pipeline/chapter-pipeline``, ``chapter-ingest/chapter-ingest`` and
``chapter-understand/chapter-understand`` (the API starts the latter two through Prefect's REST
API). One heavy flow at a time (``limit=1``): ingest and the local vision model are CPU- and
RAM-heavy.
"""

from __future__ import annotations

from typing import cast

from prefect import serve
from prefect.deployments.runner import RunnerDeployment

from orchestration.flows import chapter_pipeline
from orchestration.ingest_flow import FLOW_NAME, ingest_flow
from orchestration.understand_flow import FLOW_NAME as UNDERSTAND_FLOW_NAME
from orchestration.understand_flow import understand_flow


def main() -> None:
    # to_deployment is sync here (no running event loop); its stub also allows a coroutine.
    pipeline = cast(RunnerDeployment, chapter_pipeline.to_deployment(name="chapter-pipeline"))
    ingest = cast(RunnerDeployment, ingest_flow.to_deployment(name=FLOW_NAME, concurrency_limit=1))
    understand = cast(
        RunnerDeployment,
        understand_flow.to_deployment(name=UNDERSTAND_FLOW_NAME, concurrency_limit=1),
    )
    serve(pipeline, ingest, understand, limit=1)


if __name__ == "__main__":
    main()
