"""Start Prefect flow runs over its REST API (internal network only; Prefect has no auth here)."""

from __future__ import annotations

import logging
import uuid

import httpx

log = logging.getLogger(__name__)

INGEST_DEPLOYMENT = "chapter-ingest/chapter-ingest"  # flow name / deployment name (ruling R2)
UNDERSTAND_DEPLOYMENT = "chapter-understand/chapter-understand"
TIMEOUT = httpx.Timeout(10.0, connect=3.0)


class PrefectError(RuntimeError):
    """Prefect did not accept the run. Details are logged, never shown to the user."""


def start_ingest(chapter_id: str, api_url: str, *, timeout: httpx.Timeout = TIMEOUT) -> str:
    """Create a run of the chapter-ingest deployment; returns the Prefect flow-run id."""
    return start_deployment(INGEST_DEPLOYMENT, {"chapter_id": chapter_id}, api_url, timeout=timeout)


def start_deployment(
    deployment: str,
    parameters: dict[str, str | int],
    api_url: str,
    *,
    timeout: httpx.Timeout = TIMEOUT,
) -> str:
    """Create a run of ``deployment`` ("flow/deployment"); returns the Prefect flow-run id.

    Prefect creates runs by deployment id only (``POST /deployments/{id}/create_flow_run``), so
    the deployment is looked up by name first (``GET /deployments/name/{flow}/{deployment}``).
    ``deployment`` is a fixed constant of this package, never user input.
    """
    base = api_url.rstrip("/")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            found = client.get(f"{base}/deployments/name/{deployment}")
            if found.status_code != 200:
                log.warning("prefect deployment lookup returned HTTP %d", found.status_code)
                raise PrefectError("prefect deployment not found")
            deployment_id = _uuid(found, "deployment lookup")
            resp = client.post(
                f"{base}/deployments/{deployment_id}/create_flow_run",
                json={"parameters": parameters},
            )
    except httpx.HTTPError as exc:
        log.warning("prefect create_flow_run failed: %s", type(exc).__name__)
        raise PrefectError("prefect unreachable") from exc
    if resp.status_code not in (200, 201):
        log.warning("prefect create_flow_run returned HTTP %d", resp.status_code)
        raise PrefectError("prefect refused the run")
    return _uuid(resp, "create_flow_run")


def _uuid(resp: httpx.Response, what: str) -> str:
    """The ``id`` of a Prefect response as a canonical UUID (never trusted as a path part)."""
    try:
        return str(uuid.UUID(str(resp.json()["id"])))
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("prefect %s returned an unexpected body", what)
        raise PrefectError("unexpected prefect response") from exc
