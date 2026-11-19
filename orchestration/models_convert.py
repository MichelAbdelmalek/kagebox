"""One-shot job: convert quarantined pickle checkpoints to safetensors, offline.

Runs as the `models-convert` compose service: no network, quarantine volume read-only, output to
the models volume. Each source must match its pinned sha256; each output must equal the pinned
output hash (once pinned) and, when configured, an independent reference copy tensor for tensor.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import torch
from safetensors.torch import load_file

from orchestration.models_fetch import REFERENCE_DIR
from pipeline.providers.convert import ConversionRefused, _sha256, convert_checkpoint
from pipeline.providers.registry import load_yaml
from pipeline.providers.weights import Conversion, load_conversions, validate_content

__all__ = ["load_conversions", "main", "promote_outputs", "run_conversion"]

log = logging.getLogger("models_convert")


def _resolve(dotted: str) -> Any:
    module, _, attr = dotted.rpartition(".")
    return getattr(importlib.import_module(module), attr)


def matches_reference(
    ours: dict[str, torch.Tensor], ref: dict[str, torch.Tensor], tied: dict[str, str]
) -> bool:
    """Every reference tensor must be identical in ours. An extra tensor of ours is allowed only
    when the config declares it tied to a named reference tensor and it equals that tensor."""

    def same(a: torch.Tensor, b: torch.Tensor) -> bool:
        return a.dtype == b.dtype and a.shape == b.shape and torch.equal(a, b)

    if any(k not in ours or not same(ours[k], v) for k, v in ref.items()):
        return False
    return all(
        k in tied and tied[k] in ref and same(ours[k], ref[tied[k]]) for k in set(ours) - set(ref)
    )


def promote_outputs(c: Conversion, staging: Path, models: Path) -> list[Path]:
    """Copy converted files into the runtime models volume, only if each output hash is pinned
    and matches. Never parses pickles: this runs outside the conversion sandbox."""
    out: list[Path] = []
    for dst, pinned in c.outputs.items():
        if pinned is None:
            raise ConversionRefused(f"{c.name}: {dst} is not pinned; review and pin it first")
        src = staging / c.name / c.revision / dst
        if not src.exists() or _sha256(src) != pinned:
            raise ConversionRefused(f"{c.name}: {dst} in staging does not match its pinned hash")
        validate_content(src, "safetensors")
        target = models / c.name / c.revision / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            shutil.copyfile(src, part)
            if _sha256(part) != pinned:
                raise ConversionRefused(f"{c.name}: {dst} changed while copying")
            if target.exists():
                target.chmod(0o644)
            os.replace(part, target)
            target.chmod(0o444)
        finally:
            part.unlink(missing_ok=True)
        out.append(target)
    return out


def run_conversion(c: Conversion, quarantine: Path, models: Path) -> dict[str, str]:
    qdir = quarantine / c.name / c.revision
    for s in c.sources:
        p = qdir / s.path
        if not p.exists() or p.stat().st_size != s.size_bytes or _sha256(p) != s.sha256:
            raise ConversionRefused(f"{c.name}: source {s.path} missing or does not match its pin")
    out_dir = models / c.name / c.revision
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = [_resolve(g) for g in c.safe_globals]
    report: dict[str, str] = {}
    for step in c.steps:
        target = out_dir / step.dst
        staging = target.with_name(target.name + ".staging")
        try:
            rec = convert_checkpoint(qdir / step.src, staging, key=step.key, safe_globals=safe)
            pinned = c.outputs.get(step.dst)
            if pinned is not None and rec.output_sha256 != pinned:
                raise ConversionRefused(
                    f"{c.name}: output hash for {step.dst} does not match its pin"
                )
            if c.reference and c.reference.step_dst == step.dst:
                ref_path = qdir / REFERENCE_DIR / c.reference.path
                if not ref_path.exists() or _sha256(ref_path) != c.reference.sha256:
                    raise ConversionRefused(f"{c.name}: reference copy missing or not as pinned")
                if not matches_reference(load_file(str(staging)), load_file(str(ref_path)), c.tied):
                    raise ConversionRefused(
                        f"{c.name}: converted tensors differ from the reference"
                    )
            os.replace(staging, target)
            target.chmod(0o444)
            report[step.dst] = rec.output_sha256
        finally:
            staging.unlink(missing_ok=True)
    return report


def main(argv: list[str] | None = None) -> int:
    """`convert` (default): sandbox, pickle sources -> staging volume.
    `promote`: no pickle parsing, pinned staging outputs -> runtime models volume."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    mode = (argv if argv is not None else sys.argv[1:] or ["convert"])[0]
    staging = Path(os.environ.get("KAGEBOX_STAGING_DIR", "/staging"))
    conversions = [c for c in load_conversions(load_yaml("models.yaml")) if c.status == "approved"]
    try:
        if mode == "promote":
            models = Path(os.environ.get("KAGEBOX_MODELS_DIR", "/models"))
            for c in conversions:
                for p in promote_outputs(c, staging, models):
                    log.info("promoted %s", p)
            return 0
        if mode != "convert":
            log.error("unknown mode %r (convert | promote)", mode)
            return 2
        quarantine = Path(os.environ.get("KAGEBOX_QUARANTINE_DIR", "/quarantine"))
        reports = {c.name: run_conversion(c, quarantine, staging) for c in conversions}
    except ConversionRefused as exc:
        log.error("%s", exc)
        return 1
    for c in conversions:
        unpinned = [d for d, h in c.outputs.items() if h is None]
        if unpinned:
            log.warning("%s: review, then pin these output hashes: %s", c.name, unpinned)
    print(json.dumps(reports, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
