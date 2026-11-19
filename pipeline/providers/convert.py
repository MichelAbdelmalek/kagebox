"""Offline pickle → safetensors conversion. Runs ONLY inside the models-convert sandbox.

The sandbox is a throwaway container with no network, read-only input and a non-root user
(deploy/compose.yaml: models-convert). Even there, checkpoints are unpickled with
``weights_only=True`` and only explicitly named safe globals, so code embedded in a file is
refused rather than executed. The output is re-read and compared bit for bit before it is kept.
Runtime workers never call torch.load: they read the resulting .safetensors files only.
"""

from __future__ import annotations

import hashlib
import json
import os

# pickle is imported only for pickle.UnpicklingError; loading always uses weights_only=True.
import pickle  # nosec B403
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
from safetensors.torch import load_file, save_file

CONVERTER = "kagebox-convert/1"
MIN_TORCH = (2, 6)
PRIMITIVES = (int, float, str, bool, type(None))


class ConversionRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class ConversionRecord:
    source_sha256: str
    output_sha256: str
    tensor_count: int
    torch_version: str


def _torch_version() -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", torch.__version__)[:3])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _flatten(obj: Mapping[str, Any], prefix: str = "") -> dict[str, torch.Tensor]:
    out: dict[str, torch.Tensor] = {}
    for k, v in obj.items():
        name = f"{prefix}{k}"
        if isinstance(v, torch.Tensor):
            # clone: safetensors refuses tensors that share storage (tied weights)
            out[name] = v.detach().clone().contiguous()
        elif isinstance(v, Mapping):
            out.update(_flatten(v, f"{name}."))
        else:
            raise ConversionRefused(f"{name}: not a tensor ({type(v).__name__})")
    return out


def convert_checkpoint(
    src: Path,
    dst: Path,
    *,
    key: str | None = None,
    safe_globals: Iterable[Any] = (),
) -> ConversionRecord:
    if os.environ.get("KAGEBOX_CONVERT_SANDBOX") != "1":
        raise ConversionRefused("conversion only runs inside the models-convert sandbox")
    if _torch_version() < MIN_TORCH:
        raise ConversionRefused("torch >= 2.6 is required for a safe weights_only load")

    try:
        with torch.serialization.safe_globals(list(safe_globals)):
            obj = torch.load(src, map_location="cpu", weights_only=True)
    except (pickle.UnpicklingError, RuntimeError, AttributeError, TypeError) as exc:
        raise ConversionRefused(f"{src.name}: unsafe or unreadable checkpoint: {exc}") from exc

    meta: dict[str, Any] = {}
    if key is not None:
        if not isinstance(obj, Mapping) or key not in obj:
            raise ConversionRefused(f"{src.name}: no {key!r} entry")
        meta = {k: v for k, v in obj.items() if k != key and isinstance(v, PRIMITIVES)}
        obj = obj[key]
    if not isinstance(obj, Mapping):
        raise ConversionRefused(f"{src.name}: expected a mapping of tensors")
    tensors = _flatten(obj)
    if not tensors:
        raise ConversionRefused(f"{src.name}: no tensors found")

    source_sha = _sha256(src)
    part = dst.with_name(dst.name + ".part")
    try:
        # ONE metadata key: safetensors writes metadata from a hash map in random order, so
        # several keys would make every run's bytes (and pinned hash) different.
        # Nothing environment-specific (e.g. the torch version) goes into the file, so the pinned
        # output hash survives upgrades; the version is reported in ConversionRecord instead.
        provenance = {"converter": CONVERTER, "source_sha256": source_sha, "extra": meta}
        save_file(tensors, str(part), metadata={"kagebox": json.dumps(provenance, sort_keys=True)})
        back = load_file(str(part))
        if set(back) != set(tensors) or not all(torch.equal(back[k], tensors[k]) for k in tensors):
            raise ConversionRefused(f"{src.name}: round-trip mismatch")
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)
    return ConversionRecord(source_sha, _sha256(dst), len(tensors), torch.__version__)
