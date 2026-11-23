"""Load every approved local model offline from the read-only models volume and run one tiny
CPU inference. Proves the pinned files, the transformers version and the image work together.

    make check-models

Runs with HF offline mode and no network; models load from local files with use_safetensors,
so a missing or pickle-only model fails here instead of silently downloading something.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable

import numpy as np
import torch
from PIL import Image

from orchestration.models_fetch import load_models
from pipeline.providers.local import load
from pipeline.providers.registry import load_yaml
from pipeline.providers.weights import load_conversions

log = logging.getLogger("check_models")


def _image(w: int = 640, h: int = 480) -> Image.Image:
    rng = np.random.default_rng(0)
    return Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8))


def siglip(name: str) -> str:
    from transformers import AutoImageProcessor, SiglipVisionModel

    proc, model = (
        load(AutoImageProcessor, name, weights=False),
        load(SiglipVisionModel, name),
    )
    out = model(**proc(images=_image(), return_tensors="pt"))
    return f"embedding {tuple(out.pooler_output.shape)}"


def sam2(name: str) -> str:
    from transformers import Sam2Model, Sam2Processor

    proc, model = (
        load(Sam2Processor, name, weights=False),
        load(Sam2Model, name),
    )
    inputs = proc(
        images=_image(), input_points=[[[[320, 240]]]], input_labels=[[[1]]], return_tensors="pt"
    )
    out = model(**inputs, multimask_output=False)
    return f"mask {tuple(out.pred_masks.shape)}"


def depth(name: str) -> str:
    from transformers import AutoImageProcessor, AutoModelForDepthEstimation

    proc = load(AutoImageProcessor, name, weights=False)
    model = load(AutoModelForDepthEstimation, name)
    out = model(**proc(images=_image(), return_tensors="pt"))
    return f"depth {tuple(out.predicted_depth.shape)}"


def detector(name: str) -> str:
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    proc = load(AutoImageProcessor, name, weights=False)
    model = load(AutoModelForObjectDetection, name)
    out = model(**proc(images=_image(), return_tensors="pt"))
    # RT-DETR returns logits; the PP-OCRv5 text detector returns a probability map instead
    name, first = next((k, v) for k, v in out.items() if isinstance(v, torch.Tensor))
    return f"{name} {tuple(first.shape)}"


def generic(name: str) -> str:
    from transformers import AutoImageProcessor, AutoModel

    proc = load(AutoImageProcessor, name, weights=False)
    model = load(AutoModel, name)
    out = model(**proc(images=_image(320, 48), return_tensors="pt"))
    first = next(v for v in out.values() if isinstance(v, torch.Tensor))
    return f"{type(model).__name__} output {tuple(first.shape)}"


def whisper(name: str) -> str:
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    proc = load(WhisperProcessor, name, weights=False)
    model = load(WhisperForConditionalGeneration, name)
    feats = proc(np.zeros(16_000, dtype=np.float32), sampling_rate=16_000, return_tensors="pt")
    ids = model.generate(feats.input_features, max_new_tokens=5)
    return f"generated {tuple(ids.shape)}"


def manga_ocr(name: str) -> str:
    from transformers import AutoImageProcessor, AutoTokenizer, VisionEncoderDecoderModel

    proc = load(AutoImageProcessor, name, weights=False)
    # OCR only decodes ids -> characters; skip MeCab word splitting (no fugashi/unidic needed)
    tok = load(AutoTokenizer, name, weights=False, do_word_tokenize=False)
    model = load(VisionEncoderDecoderModel, name)
    ids = model.generate(
        proc(images=_image(64, 256), return_tensors="pt").pixel_values, max_new_tokens=5
    )
    return f"decoded {tok.decode(ids[0], skip_special_tokens=True)!r}"


def vlm(name: str) -> str:
    from transformers import AutoModelForImageTextToText, AutoProcessor

    proc = load(AutoProcessor, name, weights=False)
    # bfloat16: fp32 2B weights (8.6 GB) do not fit the models-check memory limit
    model = load(AutoModelForImageTextToText, name, dtype=torch.bfloat16)
    image = Image.new("RGB", (64, 64), (128, 128, 128))
    messages = [
        {
            "role": "user",
            "content": [{"type": "image"}, {"type": "text", "text": "Describe this image."}],
        }
    ]
    prompt = proc.apply_chat_template(messages, add_generation_prompt=True)
    inputs = proc(text=[prompt], images=[image], return_tensors="pt")
    ids = model.generate(**inputs, max_new_tokens=5, do_sample=False)
    return f"generated {tuple(ids.shape)}"


CHECKS: dict[str, Callable[[str], str]] = {
    "siglip-so400m": siglip,
    "sam2-1-hiera-small": sam2,
    "depth-anything-v2-small": depth,
    "manga-bubble-detector": detector,
    "pp-ocrv5-det": detector,  # registered as an object detector in transformers
    "pp-ocrv5-rec-en": generic,
    "pp-ocrv5-rec-ko": generic,
    "whisper-large-v3-turbo": whisper,
    "manga-ocr": manga_ocr,
    "qwen3-vl-2b-instruct": vlm,
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    torch.set_grad_enabled(False)
    raw = load_yaml("models.yaml")
    names = [m.name for m in load_models(raw) if m.status == "approved"]
    names += [c.name for c in load_conversions(raw) if c.status == "approved"]
    failed = 0
    for name in names:
        check = CHECKS.get(name)
        if check is None:
            log.error("%s: no load check defined", name)
            failed += 1
            continue
        t = time.monotonic()
        try:
            detail = check(name)  # load() re-verifies every pinned file before loading
            log.info("ok   %-26s %-40s %.1fs", name, detail, time.monotonic() - t)
        except Exception as exc:  # report every model, then fail at the end
            log.error("FAIL %-26s %s: %s", name, type(exc).__name__, str(exc)[:300])
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
