"""Local vision-language provider: one pinned model, CPU, offline, greedy decoding.

Everything it is given is untrusted: OCR text enters a prompt only through fence_untrusted,
and neither prompts nor model output are ever logged or put in an exception.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

import torch
from PIL import Image

from pipeline.common.uploads import UploadRejected, open_image_checked
from pipeline.providers import local
from pipeline.providers.base import M, ModelLoadFailed, ModelSpec, fence_untrusted
from pipeline.providers.structured import structured_from_text
from pipeline.understand.settings import UnderstandSettings

__all__ = ["REPAIR_LINE", "LocalVlmProvider"]

log = logging.getLogger(__name__)

REPAIR_LINE = "Reply with one JSON object only, matching the example."

_CROP_FORMATS = ("PNG",)  # what ingest writes for panel crops

_DTYPES = {"bfloat16": torch.bfloat16, "float32": torch.float32}

Loader = Callable[..., Any]


class LocalVlmProvider:
    def __init__(self, settings: UnderstandSettings, *, loader: Loader = local.load) -> None:
        self._s = settings
        self._loader = loader
        self._loaded: dict[str, tuple[Any, Any]] = {}
        self._threads_set = False

    # -- loading ------------------------------------------------------------------------

    def _ensure(self, spec: ModelSpec) -> tuple[Any, Any]:
        if spec.provider != "local":
            raise ValueError(f"LocalVlmProvider cannot serve provider {spec.provider!r}")
        cached = self._loaded.get(spec.model)
        if cached is not None:
            return cached
        failure: str | None = None
        try:
            from transformers import AutoModelForImageTextToText, AutoProcessor

            processor = self._loader(AutoProcessor, spec.model, weights=False)
            model = self._loader(
                AutoModelForImageTextToText, spec.model, dtype=_DTYPES[self._s.dtype]
            )
            if not self._threads_set:
                torch.set_num_threads(self._s.threads)
                self._threads_set = True
        except Exception as e:  # incl. MemoryError, UnsafeWeights, ModelNotApproved
            failure = type(e).__name__
        if failure is not None:
            log.error("model load failed: %s", failure)
            raise ModelLoadFailed()
        self._loaded[spec.model] = (processor, model)
        return processor, model

    # -- images -------------------------------------------------------------------------

    def _prepare_image(self, data: bytes) -> Image.Image:
        img: Image.Image | None = None
        failure: str | None = None
        try:
            with open_image_checked(data, _CROP_FORMATS) as im:
                img = im.convert("RGB")
        except UploadRejected as e:
            failure = str(e)  # fixed messages only
        except Exception as e:
            failure = type(e).__name__
        if img is None:
            raise ValueError(f"image refused: {failure}")
        w, h = img.size
        if w * h > self._s.max_pixels:
            scale = math.sqrt(self._s.max_pixels / (w * h))
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS
            )
        return img

    # -- generation ---------------------------------------------------------------------

    def _structured(
        self,
        spec: ModelSpec,
        instructions: str,
        content: str,
        images: list[bytes],
        schema: type[M],
    ) -> M:
        processor, model = self._ensure(spec)
        prepared = [self._prepare_image(b) for b in images]
        fenced = fence_untrusted(content)

        def generate(attempt: int) -> str:
            user_text = fenced if attempt == 1 else f"{fenced}\n\n{REPAIR_LINE}"
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": [{"type": "text", "text": instructions}]},
                {
                    "role": "user",
                    "content": [
                        *({"type": "image", "image": im} for im in prepared),
                        {"type": "text", "text": user_text},
                    ],
                },
            ]
            with torch.inference_mode():
                inputs = processor.apply_chat_template(
                    messages,
                    add_generation_prompt=True,
                    tokenize=True,
                    return_dict=True,
                    return_tensors="pt",
                    enable_thinking=False,
                )
                inputs = inputs.to(model.device)
                out = model.generate(
                    **inputs,
                    do_sample=False,
                    max_new_tokens=self._s.max_new_tokens,
                    max_time=self._s.max_time_s,
                )
                new_tokens = out[:, inputs["input_ids"].shape[1] :]
                text: str = processor.batch_decode(new_tokens, skip_special_tokens=True)[0]
            log.debug("attempt %d generated %d tokens", attempt, new_tokens.shape[1])
            return text

        return structured_from_text(spec, schema, generate, self._s.max_attempts)

    def llm_structured(
        self, spec: ModelSpec, instructions: str, content: str, schema: type[M]
    ) -> M:
        return self._structured(spec, instructions, content, [], schema)

    def vlm_structured(
        self, spec: ModelSpec, instructions: str, content: str, images: list[bytes], schema: type[M]
    ) -> M:
        if len(images) != 1:
            raise ValueError("exactly one image per call")
        return self._structured(spec, instructions, content, images, schema)
