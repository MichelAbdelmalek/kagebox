"""Local vision-language provider: one pinned model, CPU, offline, greedy decoding.

Everything it is given is untrusted: OCR text enters a prompt only through fence_untrusted,
and neither prompts nor model output are ever logged or put in an exception.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

import torch
from PIL import Image

from pipeline.common.uploads import UploadRejected, open_image_checked
from pipeline.providers import local
from pipeline.providers.base import M, ModelLoadFailed, ModelSpec, fence_untrusted
from pipeline.providers.structured import structured_from_text
from pipeline.understand.settings import UnderstandSettings

__all__ = ["NO_CONTENT_LINE", "REPAIR_LINE", "LocalVlmProvider", "json_position", "stop_reason"]

log = logging.getLogger(__name__)

NO_CONTENT_LINE = "Describe this panel."  # no OCR text is sent with a panel (injection defence)
REPAIR_LINE = "Reply with one JSON object only, matching the example."

_CROP_FORMATS = ("PNG",)  # what ingest writes for panel crops

_DTYPES = {"bfloat16": torch.bfloat16, "float32": torch.float32}

Loader = Callable[..., Any]


def stop_reason(
    new_ids: list[int], eos_ids: set[int], max_new_tokens: int, seconds: float, max_time_s: int
) -> str:
    """Why one generate call ended: eos, max_new_tokens, max_time, or other. Counts only."""
    if new_ids and new_ids[-1] in eos_ids:
        return "eos"
    if len(new_ids) >= max_new_tokens:
        return "max_new_tokens"
    if seconds >= max_time_s:
        return "max_time"
    return "other"


def _char_class(c: str) -> str:
    if c == "{":
        return "open"
    if c == "}":
        return "close"
    if c == '"':
        return "quote"
    if c == ":":
        return "colon"
    if c == ",":
        return "comma"
    return "word" if c.isalnum() else "other"


def json_position(text: str) -> str:
    """Where a JSON answer ends, as a class: nesting depth, inside a string or not, and the class
    of the last non-space character. Never any of the text itself."""
    depth, in_str, esc = 0, False, False
    for c in text:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
    stripped = text.rstrip()
    last = _char_class(stripped[-1]) if stripped else "none"
    return f"depth={max(depth, 0)} str={int(in_str)} last={last}"


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
                AutoModelForImageTextToText,
                spec.model,
                dtype=_DTYPES[self._s.dtype],
                attn="eager",  # SDPA in bf16 on CPU is ~100x slower
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
        fenced = fence_untrusted(content) if content else NO_CONTENT_LINE

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
            start = time.perf_counter()
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
            seconds = time.perf_counter() - start
            ids = [int(t) for t in new_tokens[0].tolist()]
            eos = getattr(getattr(model, "generation_config", None), "eos_token_id", None)
            eos_ids = {eos} if isinstance(eos, int) else {int(e) for e in eos or ()}
            log.info(  # counts and classes only: never any generated text
                "attempt %d: tokens=%d stop=%s seconds=%.1f %s",
                attempt,
                len(ids),
                stop_reason(ids, eos_ids, self._s.max_new_tokens, seconds, self._s.max_time_s),
                seconds,
                json_position(text),
            )
            return text

        return structured_from_text(
            spec,
            schema,
            generate,
            self._s.max_attempts,
            on_invalid=lambda n, shape: log.info("attempt %d invalid: %s", n, shape),
        )

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
