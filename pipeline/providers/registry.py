"""Load pinned models from configs/models.yaml and enforce the weights/remote-code policy."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pipeline.providers.base import ModelSpec

CONFIG_DIR = Path(
    os.environ.get("KAGEBOX_CONFIG_DIR") or Path(__file__).resolve().parents[2] / "configs"
)
ALLOWED_WEIGHT_SUFFIXES = frozenset({".safetensors"})


class ModelNotApproved(LookupError):
    pass


class UnsafeWeights(PermissionError):
    pass


def load_yaml(name: str, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    data = yaml.safe_load((config_dir / name).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a mapping")
    return data


def model_for_stage(stage: str, config_dir: Path = CONFIG_DIR) -> ModelSpec:
    cfg = load_yaml("models.yaml", config_dir)
    entry = (cfg.get("stages") or {}).get(stage)
    if not entry or entry.get("status") != "approved" or not entry.get("model"):
        raise ModelNotApproved(f"no approved model for stage {stage!r} in models.yaml")
    required = ("provider", "model", "revision", "license", "source")
    missing = [k for k in required if not entry.get(k)]
    if missing:
        raise ModelNotApproved(f"stage {stage!r} is missing {missing}")
    return ModelSpec(
        stage=stage,
        provider=entry["provider"],
        model=entry["model"],
        revision=str(entry["revision"]),
        license=entry["license"],
        source_url=entry["source"],
    )


@dataclass(frozen=True)
class WeightFile:
    path: Path
    sha256: str


def verify_weight_file(wf: WeightFile) -> Path:
    """Only checksummed .safetensors files load; pickle formats (.bin/.pt/.ckpt) never do."""
    if wf.path.suffix not in ALLOWED_WEIGHT_SUFFIXES:
        raise UnsafeWeights(f"{wf.path.name}: only .safetensors weights are allowed")
    h = hashlib.sha256()
    with wf.path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != wf.sha256.lower():
        raise UnsafeWeights(f"{wf.path.name}: checksum mismatch")
    return wf.path


def trust_remote_code(repo: str, revision: str, config_dir: Path = CONFIG_DIR) -> bool:
    """True only for a repo+revision listed, with justification, in trust_remote_code.yaml."""
    for item in load_yaml("trust_remote_code.yaml", config_dir).get("allow") or []:
        if (
            item.get("repo") == repo
            and item.get("revision") == revision
            and item.get("justification")
            and item.get("reviewed_by")
        ):
            return True
    raise UnsafeWeights(f"trust_remote_code is not allowed for {repo}@{revision}")
