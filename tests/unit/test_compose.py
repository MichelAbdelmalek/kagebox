"""deploy/compose.yaml: secrets are mounted only into services whose code reads them."""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose.yaml"

# What worker code reads: the database password (pipeline.common.database) and the object-store
# keys (pipeline.common.storage). Nothing in pipeline/ or orchestration/ reads the media signing
# key, the session secret or (until a stage calls Together) the Together key, and a PDF child
# exploit in worker-cpu could read every secret mounted there.
WORKER_SECRETS = {"db_password", "s3_access_key", "s3_secret_key"}


def _services() -> dict[str, dict[str, object]]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services: dict[str, dict[str, object]] = data["services"]
    return services


def test_workers_get_only_the_secrets_they_read() -> None:
    services = _services()
    for name in ("worker-cpu", "worker-gpu"):
        secrets = set(services[name].get("secrets", []))  # type: ignore[call-overload]
        assert secrets <= WORKER_SECRETS, (name, sorted(secrets - WORKER_SECRETS))
        env = services[name].get("environment", {})
        assert "KAGEBOX_TOGETHER_API_KEY_FILE" not in env  # type: ignore[operator]
