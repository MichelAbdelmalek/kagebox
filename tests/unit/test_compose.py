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


def test_worker_cpu_has_no_egress_and_only_models_fetch_has() -> None:
    # No worker-cpu stage calls the internet: ingest, OCR and understand load pinned weights
    # from the read-only models volume, and nothing in the pipeline uses Together yet.
    services = _services()
    assert services["worker-cpu"]["networks"] == ["back"]
    with_egress = {
        name for name, svc in services.items() if "egress" in (svc.get("networks") or [])
    }
    assert with_egress == {"models-fetch"}


def test_bakeoff_service_has_no_egress_and_no_published_ports() -> None:
    svc = _services()["understand-bakeoff"]
    assert svc["profiles"] == ["bakeoff"]
    assert svc["networks"] == ["back"]  # internal: no egress, no internet
    assert "ports" not in svc and "network_mode" not in svc
    assert svc["mem_limit"] == "10g"
    assert set(svc["secrets"]) == {"db_password", "s3_access_key", "s3_secret_key"}  # type: ignore[call-overload]
    volumes = set(svc["volumes"])  # type: ignore[call-overload]
    assert "models:/models:ro" in volumes
    assert "../data/reports:/reports" in volumes
    assert not any("docker.sock" in v for v in volumes)


def test_bakeoff_target_never_rebuilds_the_image() -> None:
    root = COMPOSE.parents[1]
    makefile = (root / "Makefile").read_text()
    line = next(x for x in makefile.splitlines() if x.startswith("bakeoff-understand:"))
    assert "worker-image" not in line
    script = (root / "deploy/scripts/bakeoff.sh").read_text()
    assert "--build" not in script and "/app/pipeline:ro" in script
    assert "deploy/scripts/bakeoff.sh" in makefile


def _env(service: str) -> dict[str, object]:
    env = _services()[service].get("environment", {})
    assert isinstance(env, dict)
    return env


def test_worker_cpu_memory_fits_the_vision_model() -> None:
    # The local 2B vision model (bf16, about 4-5 GB weights plus activations) runs in worker-cpu.
    assert _services()["worker-cpu"]["mem_limit"] == "8g"


def test_workers_run_hub_offline() -> None:
    # Weights come from the read-only models volume only; nothing may reach the Hub at runtime.
    for name in ("worker-cpu", "worker-gpu"):
        env = _env(name)
        assert env.get("HF_HUB_OFFLINE") == "1", name
        assert env.get("TRANSFORMERS_OFFLINE") == "1", name
        assert "models:/models:ro" in _services()[name]["volumes"], name  # type: ignore[operator]
