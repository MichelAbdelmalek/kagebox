from __future__ import annotations

import pytest
import yaml

from pipeline.providers.local import model_dir
from pipeline.providers.registry import CONFIG_DIR, ModelNotApproved, load_yaml
from tests.unit.test_pin_models import pin_models

NOT_SELECTED = ("qwen3-5-2b", "qwen3-5-0-8b")


def sources() -> dict[str, dict[str, object]]:
    return yaml.safe_load((CONFIG_DIR / "models.sources.yaml").read_text())


def test_status_of_defaults_to_approved_and_refuses_unknown() -> None:
    assert pin_models.status_of({}) == "approved"
    assert pin_models.status_of({"status": "proposed"}) == "proposed"
    with pytest.raises(SystemExit):
        pin_models.status_of({"status": "maybe"})


def test_unselected_candidates_are_proposed_in_sources_and_pinned_file() -> None:
    models = load_yaml("models.yaml")["models"]
    for name in NOT_SELECTED:
        assert sources()[name]["status"] == "proposed"
        assert models[name]["status"] == "proposed"
    assert models["qwen3-vl-2b-instruct"]["status"] == "approved"


def test_pinned_status_matches_sources_for_every_model() -> None:
    models = load_yaml("models.yaml")["models"]
    for name, src in sources().items():
        if name in models:
            assert models[name]["status"] == src.get("status", "approved"), name


def test_model_dir_refuses_not_selected_models() -> None:
    for name in NOT_SELECTED:
        with pytest.raises(ModelNotApproved):
            model_dir(name)
