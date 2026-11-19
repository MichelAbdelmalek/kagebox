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


# The only extra from_pretrained kwargs callers may pass. Anything else (path or source
# redirects, remote code, offline/dtype/safetensors switches) is refused, not overridden.
ALLOWED_EXTRA = frozenset({"do_word_tokenize"})


def _walk(d: Path) -> list[str]:
    """Relative paths of every entry under d; symlinks (file or dir) are refused, not followed."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
        for n in (*dirnames, *filenames):
            p = Path(dirpath) / n
            if p.is_symlink():
                raise UnsafeWeights(f"{p.relative_to(d)}: symlink in model directory")
            if p.is_file():
                found.append(p.relative_to(d).as_posix())
    return found


def _only_pinned(d: Path, pinned: set[str], name: str) -> None:
    extra = sorted(set(_walk(d)) - pinned)
    if extra:
        raise UnsafeWeights(f"{name}: unpinned files in model directory: {extra[:5]}")


def model_dir(name: str, root: Path = MODELS_DIR, *, raw: dict[str, Any] | None = None) -> Path:
    """Re-verify every pinned file of an approved model or conversion; return its directory.

    The directory must contain exactly the pinned files (no extras, no symlinks), because
    from_pretrained reads anything it finds there. Raises ModelNotApproved (unknown or
    proposed) or UnsafeWeights (missing, tampered, unpinned, extra, symlinked).
    """
    cfg = raw if raw is not None else load_yaml("models.yaml")
    for m in load_models(cfg):
        if m.name == name:
            if m.status != "approved":
                raise ModelNotApproved(f"{name}: status is {m.status}, not approved")
            d = root / m.name / m.revision
            _only_pinned(d, {f.path for f in m.files}, name)
            for f in m.files:
                verify_file(d / f.path, f)
            return d
    for c in load_conversions(cfg):
        if c.name == name:
            if c.status != "approved":
                raise ModelNotApproved(f"{name}: status is {c.status}, not approved")
            d = root / c.name / c.revision
            _only_pinned(d, {f.path for f in c.data_files} | set(c.outputs), name)
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
    bad = sorted(set(extra) - ALLOWED_EXTRA)
    if bad:
        raise ValueError(f"load({name}): refused kwargs {bad}; allowed: {sorted(ALLOWED_EXTRA)}")
    d = model_dir(name)
    kwargs: dict[str, Any] = {**extra, "local_files_only": True, "trust_remote_code": False}
    if weights:
        kwargs.update(use_safetensors=True, dtype=torch.float32)
    return cls.from_pretrained(str(d), **kwargs)
