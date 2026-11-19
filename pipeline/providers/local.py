"""Verify-then-load for local models: every pinned file is re-hashed before anything is loaded.

Models load only from the read-only models volume, offline, as safetensors, never with
trust_remote_code. `revision` does not apply: there is no Hub access (local_files_only=True).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import torch

from orchestration.models_fetch import load_models
from pipeline.providers.registry import ModelNotApproved, load_yaml
from pipeline.providers.weights import (
    UnsafeWeights,
    load_conversions,
    sha256_file,
    validate_content,
    verify_file,
)

__all__ = ["MODELS_DIR", "load", "model_dir"]

MODELS_DIR = Path(os.environ.get("KAGEBOX_MODELS_DIR", "/models"))


def model_dir(name: str, root: Path = MODELS_DIR, *, raw: dict[str, Any] | None = None) -> Path:
    """Re-verify every pinned file of an approved model or conversion; return its directory.

    Raises ModelNotApproved (unknown or proposed) or UnsafeWeights (missing, tampered, unpinned).
    """
    cfg = raw if raw is not None else load_yaml("models.yaml")
    for m in load_models(cfg):
        if m.name == name:
            if m.status != "approved":
                raise ModelNotApproved(f"{name}: status is {m.status}, not approved")
            d = root / m.name / m.revision
            for f in m.files:
                verify_file(d / f.path, f)
            return d
    for c in load_conversions(cfg):
        if c.name == name:
            if c.status != "approved":
                raise ModelNotApproved(f"{name}: status is {c.status}, not approved")
            d = root / c.name / c.revision
            for f in c.data_files:
                verify_file(d / f.path, f)
            for dst, pinned in c.outputs.items():
                if pinned is None or not (d / dst).is_file() or sha256_file(d / dst) != pinned:
                    raise UnsafeWeights(f"{name}/{dst}: missing, unpinned or mismatched")
                validate_content(d / dst, "safetensors")
            return d
    raise ModelNotApproved(f"{name}: not in models.yaml")


def load(cls: Any, name: str, *, weights: bool = True, **extra: Any) -> Any:
    """`cls.from_pretrained` on the verified local directory. weights=False for processors and
    tokenizers (no tensors). float32 on CPU: some configs (Whisper) declare float16."""
    d = model_dir(name)
    kwargs: dict[str, Any] = {"local_files_only": True, **extra}
    if weights:
        kwargs.update(use_safetensors=True, trust_remote_code=False, dtype=torch.float32)
    return cls.from_pretrained(str(d), **kwargs)
