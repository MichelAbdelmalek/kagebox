"""OCR of one text-region crop, with a confidence that is a real model probability.

``ocr_for(language)`` routes Japanese to ``MangaOcr`` (manga-ocr, a ViT encoder + BERT decoder
that reads vertical and horizontal crops natively) and English / Korean to ``PpOcr`` (PP-OCRv5
mobile text detection, then CTC line recognition). Both load their pinned weights lazily, once
per instance, via verify-then-load, and run on CPU in float32 under ``torch.inference_mode()``.

Confidence:

* manga-ocr: greedy generation with ``output_scores=True``; for every generated text token the
  max softmax probability of that step; confidence = their mean (``token_confidence``). Special
  tokens (BOS/EOS/PAD) are not text and do not count.
* PP-OCRv5: transformers' ``post_process_text_recognition`` does the greedy best-path CTC decode
  (collapse repeats, drop blank) and returns each line's mean per-character max probability;
  lines are combined by character count, so the result is the mean over every character.

Empty text has confidence 0.0, so it is always flagged for review (< ``OCR_CONFIDENCE_MIN``).
OCR output is untrusted data: it is returned as a plain ``str`` with control and format
characters removed (newline kept) and capped at ``MAX_TEXT_LEN`` (``Bubble.text``'s limit).
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Sequence
from typing import Any, Literal, get_args

from PIL import Image

__all__ = [
    "MAX_TEXT_LEN",
    "MangaOcr",
    "PpOcr",
    "combine_lines",
    "ocr_for",
    "ocr_result",
    "sanitize",
    "token_confidence",
]

MAX_TEXT_LEN = 2000  # Bubble.text max_length
MANGA_OCR_MODEL = "manga-ocr"
MANGA_OCR_MAX_LENGTH = 300  # manga-ocr's own generation limit
PP_DET_MODEL = "pp-ocrv5-det"
PpLang = Literal["en", "ko"]
PP_REC_MODELS: dict[PpLang, str] = {"en": "pp-ocrv5-rec-en", "ko": "pp-ocrv5-rec-ko"}


def token_confidence(probs: Sequence[float]) -> float:
    """Mean of per-token probabilities; 0.0 for an empty sequence."""
    if not probs:
        return 0.0
    for p in probs:
        if not 0.0 <= p <= 1.0:  # also rejects NaN
            raise ValueError(f"not a probability: {p!r}")
    return math.fsum(probs) / len(probs)


def sanitize(text: str) -> str:
    """Drop control / format characters except newline, trim, cap at MAX_TEXT_LEN."""
    kept = "".join(
        c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co")
    )
    return kept.strip()[:MAX_TEXT_LEN]


def ocr_result(text: str, probs: Sequence[float]) -> tuple[str, float]:
    """Sanitised text and its confidence; nothing readable means confidence 0.0."""
    clean = sanitize(text)
    return (clean, token_confidence(probs)) if clean else ("", 0.0)


def combine_lines(lines: Sequence[tuple[str, float]]) -> tuple[str, float]:
    """Join recognised lines (top to bottom) with newlines; confidence is weighted by each
    line's character count, i.e. the mean over all characters. Empty lines are skipped."""
    kept = [(t, s) for t, s in lines if t]
    if not kept:
        return "", 0.0
    probs = [s for t, s in kept for _ in t]
    return ocr_result("\n".join(t for t, _ in kept), probs)


class MangaOcr:
    """manga-ocr for Japanese crops (vertical or horizontal)."""

    engine = MANGA_OCR_MODEL

    def __init__(self) -> None:
        self._parts: tuple[Any, Any, Any] | None = None

    def _load(self) -> tuple[Any, Any, Any]:
        if self._parts is None:
            from transformers import AutoImageProcessor, AutoTokenizer, VisionEncoderDecoderModel

            from pipeline.providers.local import load

            proc = load(AutoImageProcessor, MANGA_OCR_MODEL, weights=False)
            # decoding ids -> characters only; MeCab word splitting is not needed
            tok = load(AutoTokenizer, MANGA_OCR_MODEL, weights=False, do_word_tokenize=False)
            model = load(VisionEncoderDecoderModel, MANGA_OCR_MODEL)
            model.eval()
            self._parts = (proc, tok, model)
        return self._parts

    def read(self, crop: Image.Image) -> tuple[str, float]:
        import torch

        proc, tok, model = self._load()
        img = crop.convert("L").convert("RGB")  # manga-ocr's own preprocessing
        with torch.inference_mode():
            pixel_values = proc(images=img, return_tensors="pt").pixel_values
            out = model.generate(
                pixel_values,
                max_length=MANGA_OCR_MAX_LENGTH,
                num_beams=1,
                do_sample=False,
                output_scores=True,
                return_dict_in_generate=True,
            )
        # out.sequences[0] = [decoder start, tok_1 .. tok_n]; out.scores[i] produced tok_{i+1}
        generated = out.sequences[0, -len(out.scores) :].tolist()
        special = set(tok.all_special_ids)
        probs = [
            float(torch.softmax(step[0].float(), dim=-1).max())
            for tid, step in zip(generated, out.scores, strict=True)
            if tid not in special
        ]
        text = tok.decode([t for t in generated if t not in special], skip_special_tokens=True)
        # the char-level tokenizer joins characters with spaces; manga-ocr removes all whitespace
        return ocr_result("".join(text.split()), probs)


class PpOcr:
    """PP-OCRv5 mobile: text-line detection, then CTC recognition per line, top to bottom."""

    def __init__(self, lang: PpLang) -> None:
        if lang not in get_args(PpLang):
            raise ValueError(f"PP-OCRv5 language not supported: {lang!r}")
        self.lang: PpLang = lang
        self.engine = PP_REC_MODELS[lang]
        self._parts: tuple[Any, Any, Any, Any] | None = None

    def _load(self) -> tuple[Any, Any, Any, Any]:
        if self._parts is None:
            from transformers import (
                AutoImageProcessor,
                AutoModelForObjectDetection,
                AutoModelForTextRecognition,
            )

            from pipeline.providers.local import load

            det_proc = load(AutoImageProcessor, PP_DET_MODEL, weights=False)
            det = load(AutoModelForObjectDetection, PP_DET_MODEL)
            rec_name = PP_REC_MODELS[self.lang]
            rec_proc = load(AutoImageProcessor, rec_name, weights=False)
            if not rec_proc.character_list:  # the CTC dictionary ships in the pinned files
                raise RuntimeError(f"{rec_name}: no character_list in preprocessor_config.json")
            rec = load(AutoModelForTextRecognition, rec_name)
            det.eval()
            rec.eval()
            self._parts = (det_proc, det, rec_proc, rec)
        return self._parts

    def _lines(self, img: Image.Image) -> list[tuple[int, int, int, int]]:
        """Axis-aligned (x0, y0, x1, y1) text-line boxes, top to bottom then left to right."""
        det_proc, det, _, _ = self._load()
        inputs = det_proc(images=img, return_tensors="pt")
        sizes = inputs.pop("target_sizes")
        [res] = det_proc.post_process_object_detection(det(**inputs), target_sizes=sizes)
        boxes: list[tuple[int, int, int, int]] = []
        for quad in res["boxes"].tolist():  # (4, 2) corner points, clamped to the image
            xs, ys = [int(p[0]) for p in quad], [int(p[1]) for p in quad]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            if x1 > x0 and y1 > y0:
                boxes.append((x0, y0, x1, y1))
        return sorted(boxes, key=lambda b: (b[1], b[0]))

    def read(self, crop: Image.Image) -> tuple[str, float]:
        import torch

        _, _, rec_proc, rec = self._load()
        img = crop.convert("RGB")
        lines: list[tuple[str, float]] = []
        with torch.inference_mode():
            for box in self._lines(img):
                inputs = rec_proc(images=img.crop(box), return_tensors="pt")
                [res] = rec_proc.post_process_text_recognition(rec(**inputs))
                lines.append((str(res["text"]), float(res["score"])))
        return combine_lines(lines)


def ocr_for(language: str) -> MangaOcr | PpOcr:
    """The OCR engine for a source language code ('ja', 'en' or 'ko')."""
    if language == "ja":
        return MangaOcr()
    if language in ("en", "ko"):
        return PpOcr(language)  # type: ignore[arg-type]
    raise ValueError(f"no OCR engine for language {language!r}")
