"""Load every approved local model offline from the read-only models volume and run one tiny
CPU inference. Proves the pinned files, the transformers version and the image work together.

    make check-models

Runs with HF offline mode and no network; models load from local files with use_safetensors,
so a missing or pickle-only model fails here instead of silently downloading something.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image

from orchestration.models_fetch import load_models
from pipeline.providers.registry import load_yaml
from pipeline.providers.weights import (
    Conversion,
    LocalModel,
    UnsafeWeights,
    load_conversions,
    sha256_file,
    validate_content,
    verify_file,
)

log = logging.getLogger("check_models")


def _load(cls: Any, d: Path, *, weights: bool = True, **extra: Any) -> Any:
    """Load from a LOCAL, hash-verified directory only. `revision` does not apply: no Hub access
    (HF_HUB_OFFLINE=1, local_files_only=True, and the container has no network)."""
    kwargs: dict[str, Any] = {"local_files_only": True, **extra}
    if weights:
        # float32 on CPU: some configs (Whisper) declare float16, which CPU kernels reject
        kwargs.update(use_safetensors=True, trust_remote_code=False, dtype=torch.float32)
    return cls.from_pretrained(str(d), **kwargs)


def _image(w: int = 640, h: int = 480) -> Image.Image:
    rng = np.random.default_rng(0)
    return Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8))


def siglip(d: Path) -> str:
    from transformers import AutoImageProcessor, SiglipVisionModel

    proc, model = (
        _load(AutoImageProcessor, d, weights=False),
        _load(SiglipVisionModel, d),
    )
    out = model(**proc(images=_image(), return_tensors="pt"))
    return f"embedding {tuple(out.pooler_output.shape)}"


def sam2(d: Path) -> str:
    from transformers import Sam2Model, Sam2Processor

    proc, model = (
        _load(Sam2Processor, d, weights=False),
        _load(Sam2Model, d),
    )
    inputs = proc(
        images=_image(), input_points=[[[[320, 240]]]], input_labels=[[[1]]], return_tensors="pt"
    )
    out = model(**inputs, multimask_output=False)
    return f"mask {tuple(out.pred_masks.shape)}"


def depth(d: Path) -> str:
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    proc = _load(AutoImageProcessor, d, weights=False)
    model = _load(AutoModelForDepthEstimation, d)
    out = model(**proc(images=_image(), return_tensors="pt"))
    return f"depth {tuple(out.predicted_depth.shape)}"


def detector(d: Path) -> str:
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    proc = _load(AutoImageProcessor, d, weights=False)
    model = _load(AutoModelForObjectDetection, d)
    out = model(**proc(images=_image(), return_tensors="pt"))
    # RT-DETR returns logits; the PP-OCRv5 text detector returns a probability map instead
    name, first = next((k, v) for k, v in out.items() if isinstance(v, torch.Tensor))
    return f"{name} {tuple(first.shape)}"


def generic(d: Path) -> str:
    from transformers import AutoImageProcessor, AutoModel

    proc = _load(AutoImageProcessor, d, weights=False)
    model = _load(AutoModel, d)
    out = model(**proc(images=_image(320, 48), return_tensors="pt"))
    first = next(v for v in out.values() if isinstance(v, torch.Tensor))
    return f"{type(model).__name__} output {tuple(first.shape)}"


def whisper(d: Path) -> str:
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    proc = _load(WhisperProcessor, d, weights=False)
    model = _load(WhisperForConditionalGeneration, d)
    feats = proc(np.zeros(16_000, dtype=np.float32), sampling_rate=16_000, return_tensors="pt")
    ids = model.generate(feats.input_features, max_new_tokens=5)
    return f"generated {tuple(ids.shape)}"


def manga_ocr(d: Path) -> str:
    from transformers import AutoImageProcessor, AutoTokenizer, VisionEncoderDecoderModel

    proc = _load(AutoImageProcessor, d, weights=False)
    # OCR only decodes ids -> characters; skip MeCab word splitting (no fugashi/unidic needed)
    tok = _load(AutoTokenizer, d, weights=False, do_word_tokenize=False)
    model = _load(VisionEncoderDecoderModel, d)
    ids = model.generate(
        proc(images=_image(64, 256), return_tensors="pt").pixel_values, max_new_tokens=5
    )
    return f"decoded {tok.decode(ids[0], skip_special_tokens=True)!r}"


def verify_installed(m: LocalModel, root: Path) -> Path:
    """Re-check every pinned file of an installed model before it is loaded."""
    d = root / m.name / m.revision
    for f in m.files:
        verify_file(d / f.path, f)
    return d


def verify_converted(c: Conversion, root: Path) -> Path:
    d = root / c.name / c.revision
    for f in c.data_files:
        verify_file(d / f.path, f)
    for dst, pinned in c.outputs.items():
        if pinned is None or sha256_file(d / dst) != pinned:
            raise UnsafeWeights(f"{c.name}/{dst}: missing, unpinned or does not match its pin")
        validate_content(d / dst, "safetensors")
    return d


CHECKS: dict[str, Callable[[Path], str]] = {
    "siglip-so400m": siglip,
    "sam2-1-hiera-small": sam2,
    "depth-anything-v2-small": depth,
    "manga-bubble-detector": detector,
    "pp-ocrv5-det": detector,  # registered as an object detector in transformers
    "pp-ocrv5-rec-en": generic,
    "pp-ocrv5-rec-ko": generic,
    "whisper-large-v3-turbo": whisper,
    "manga-ocr": manga_ocr,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    torch.set_grad_enabled(False)
    root = Path(os.environ.get("KAGEBOX_MODELS_DIR", "/models"))
    raw = load_yaml("models.yaml")
    targets: list[tuple[str, Callable[[], Path]]] = [
        (m.name, lambda m=m: verify_installed(m, root))  # type: ignore[misc]
        for m in load_models(raw)
        if m.status == "approved"
    ]
    targets += [
        (c.name, lambda c=c: verify_converted(c, root))  # type: ignore[misc]
        for c in load_conversions(raw)
        if c.status == "approved"
    ]
    failed = 0
    for name, verify in targets:
        check = CHECKS.get(name)
        if check is None:
            log.error("%s: no load check defined", name)
            failed += 1
            continue
        t = time.monotonic()
        try:
            detail = check(verify())  # hashes re-checked before anything is loaded
            log.info("ok   %-26s %-40s %.1fs", name, detail, time.monotonic() - t)
        except Exception as exc:  # report every model, then fail at the end
            log.error("FAIL %-26s %s: %s", name, type(exc).__name__, str(exc)[:300])
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
