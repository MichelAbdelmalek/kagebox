"""Start Prefect flow runs over its REST API (internal network only; Prefect has no auth here)."""

from __future__ import annotations

import logging
import uuid

import httpx

log = logging.getLogger(__name__)

INGEST_DEPLOYMENT = "chapter-ingest/chapter-ingest"  # flow name / deployment name (ruling R2)
TIMEOUT = httpx.Timeout(10.0, connect=3.0)


class PrefectError(RuntimeError):
    """Prefect did not accept the run. Details are logged, never shown to the user."""


def start_ingest(chapter_id: str, api_url: str, *, timeout: httpx.Timeout = TIMEOUT) -> str:
    """Create a run of the chapter-ingest deployment; returns the Prefect flow-run id."""
    url = f"{api_url.rstrip('/')}/deployments/name/{INGEST_DEPLOYMENT}/create_flow_run"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = client.post(url, json={"parameters": {"chapter_id": chapter_id}})
    except httpx.HTTPError as exc:
        log.warning("prefect create_flow_run failed: %s", type(exc).__name__)
        raise PrefectError("prefect unreachable") from exc
    if resp.status_code not in (200, 201):
        log.warning("prefect create_flow_run returned HTTP %d", resp.status_code)
        raise PrefectError("prefect refused the run")
    try:
        run_id = str(uuid.UUID(str(resp.json()["id"])))
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("prefect create_flow_run returned an unexpected body")
        raise PrefectError("unexpected prefect response") from exc
    return run_id
