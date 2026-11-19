"""Worker entry point: serve the chapter flows to the Prefect server on the internal network.

Deployments: ``chapter-pipeline/chapter-pipeline`` and ``chapter-ingest/chapter-ingest`` (the API
starts the latter through Prefect's REST API). One ingest at a time: it is CPU- and RAM-heavy.
"""

from __future__ import annotations

from typing import cast

from prefect import serve
from prefect.deployments.runner import RunnerDeployment

from orchestration.flows import chapter_pipeline
from orchestration.ingest_flow import FLOW_NAME, ingest_flow


def main() -> None:
    # to_deployment is sync here (no running event loop); its stub also allows a coroutine.
    pipeline = cast(RunnerDeployment, chapter_pipeline.to_deployment(name="chapter-pipeline"))
    ingest = cast(RunnerDeployment, ingest_flow.to_deployment(name=FLOW_NAME, concurrency_limit=1))
    serve(pipeline, ingest, limit=2)


if __name__ == "__main__":
    main()
