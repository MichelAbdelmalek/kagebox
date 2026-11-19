"""OCR of one text-region crop, with a confidence that is a real model probability.

``ocr_for(language)`` routes Japanese to ``MangaOcr`` (manga-ocr, a ViT encoder + BERT decoder
that reads vertical and horizontal crops natively) and English / Korean to ``PpOcr`` (PP-OCRv5
mobile text detection, then CTC line recognition). Both load their pinned weights lazily, once
per instance, via verify-then-load, and run on CPU in float32 under ``torch.inference_mode()``.

Confidence:

* manga-ocr: greedy generation with ``output_scores=True``; for every generated text token the
  max softmax probability of that step; confidence = their mean (``token_confidence``).
  BOS/EOS/PAD are structure and do not count; any other special token (``[UNK]``, ``[MASK]``)
  means a character could not be read: it becomes U+FFFD in the text and contributes 0.0.
* PP-OCRv5: transformers' ``post_process_text_recognition`` does the greedy best-path CTC decode
  (collapse repeats, drop blank) and returns each box's mean per-character max probability.
  Boxes are grouped into lines by vertical overlap (``order_boxes``); boxes of a line are joined
  by a space, lines by a newline. Confidence is the mean over every character, and a detected
  box that recognises to nothing contributes one 0.0, so it is never dropped silently.

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
    "REPLACEMENT",
    "MangaOcr",
    "PpOcr",
    "combine_lines",
    "ocr_for",
    "ocr_result",
    "order_boxes",
    "sanitize",
    "token_confidence",
]

MAX_TEXT_LEN = 2000  # Bubble.text max_length
REPLACEMENT = "\ufffd"  # stands in for a character the model could not read ([UNK])
MANGA_OCR_MODEL = "manga-ocr"
MANGA_OCR_MAX_LENGTH = 300  # manga-ocr's own generation limit
PP_DET_MODEL = "pp-ocrv5-det"
PpLang = Literal["en", "ko"]
PP_REC_MODELS: dict[PpLang, str] = {"en": "pp-ocrv5-rec-en", "ko": "pp-ocrv5-rec-ko"}
LINE_OVERLAP = 0.5  # share of the smaller box height two boxes must share to be one line

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels


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


def _same_line(a: Box, b: Box) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    return overlap > 0 and overlap >= LINE_OVERLAP * min(a[3] - a[1], b[3] - b[1])


def _group_lines(boxes: Sequence[Box]) -> list[list[int]]:
    """Box indices grouped into visual lines (top to bottom), each line left to right.

    Boxes are visited by top edge; a box joins the current line when it overlaps any box of
    that line vertically by at least LINE_OVERLAP of the smaller height (PaddleOCR-style)."""
    lines: list[list[int]] = []
    for i in sorted(range(len(boxes)), key=lambda k: (boxes[k][1], boxes[k][0])):
        if lines and any(_same_line(boxes[i], boxes[j]) for j in lines[-1]):
            lines[-1].append(i)
        else:
            lines.append([i])
    return [sorted(line, key=lambda k: (boxes[k][0], boxes[k][1])) for line in lines]


def order_boxes(boxes: Sequence[Box]) -> list[int]:
    """Reading order of text boxes: lines top to bottom, boxes within a line left to right."""
    return [i for line in _group_lines(boxes) for i in line]


def combine_lines(lines: Sequence[Sequence[tuple[str, float]]]) -> tuple[str, float]:
    """Recognised boxes, grouped by line, to one text: boxes of a line joined by a space, lines
    by a newline. Confidence is the mean over every character; a box that recognised to
    nothing counts as one 0.0 (it was detected but could not be read)."""
    texts: list[str] = []
    probs: list[float] = []
    for line in lines:
        words: list[str] = []
        for text, score in line:
            clean = sanitize(text)
            if clean:
                words.append(clean)
                probs.extend([score] * len(clean))
            else:
                probs.append(0.0)
        if words:
            texts.append(" ".join(words))
    return ocr_result("\n".join(texts), probs)


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

    def _tokenizer(self) -> Any:
        return self._load()[1]

    def _generate(self, img: Image.Image) -> tuple[list[int], list[float]]:
        """Greedy generation: the generated ids and, per id, the max softmax of its step."""
        import torch

        proc, _, model = self._load()
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
            # out.sequences[0] = [decoder start, tok_1 .. tok_n]; out.scores[i] produced tok_i+1
            ids = out.sequences[0, -len(out.scores) :].tolist()
            probs = [float(torch.softmax(s[0].float(), dim=-1).max()) for s in out.scores]
        return ids, probs

    def read(self, crop: Image.Image) -> tuple[str, float]:
        tok = self._tokenizer()
        ids, probs = self._generate(crop.convert("L").convert("RGB"))  # manga-ocr's preprocessing
        # BOS/EOS/PAD are structure, not text. Any other special token ([UNK], [MASK]) means a
        # character could not be read: it shows as REPLACEMENT and contributes 0.0.
        structural = {
            t
            for t in (
                tok.pad_token_id,
                tok.cls_token_id,
                tok.sep_token_id,
                tok.bos_token_id,
                tok.eos_token_id,
            )
            if t is not None
        }
        unreadable = set(tok.all_special_ids) - structural
        parts: list[str] = []
        run: list[int] = []
        kept: list[float] = []

        def flush() -> None:
            if run:
                # the char-level tokenizer joins characters with spaces; manga-ocr strips them
                parts.append("".join(tok.decode(run, skip_special_tokens=False).split()))
                run.clear()

        for tid, p in zip(ids, probs, strict=True):
            if tid in structural:
                continue
            if tid in unreadable:
                flush()
                parts.append(REPLACEMENT)
                kept.append(0.0)
            else:
                run.append(tid)
                kept.append(p)
        flush()
        return ocr_result("".join(parts), kept)


class PpOcr:
    """PP-OCRv5 mobile: text-line detection, then CTC recognition per box, in reading order."""

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

    def _detect(self, img: Image.Image) -> list[Box]:
        """Axis-aligned text boxes, in detector order."""
        import torch

        det_proc, det, _, _ = self._load()
        with torch.inference_mode():
            inputs = det_proc(images=img, return_tensors="pt")
            sizes = inputs.pop("target_sizes")
            [res] = det_proc.post_process_object_detection(det(**inputs), target_sizes=sizes)
        boxes: list[Box] = []
        for quad in res["boxes"].tolist():  # (4, 2) corner points, clamped to the image
            xs, ys = [int(p[0]) for p in quad], [int(p[1]) for p in quad]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            if x1 > x0 and y1 > y0:
                boxes.append((x0, y0, x1, y1))
        return boxes

    def _recognize(self, crop: Image.Image) -> tuple[str, float]:
        """One box: transformers' greedy CTC decode; score = mean per-character max prob."""
        import torch

        _, _, rec_proc, rec = self._load()
        with torch.inference_mode():
            inputs = rec_proc(images=crop, return_tensors="pt")
            [res] = rec_proc.post_process_text_recognition(rec(**inputs))
        return str(res["text"]), float(res["score"])

    def read(self, crop: Image.Image) -> tuple[str, float]:
        img = crop.convert("RGB")
        boxes = self._detect(img)
        lines = [
            [self._recognize(img.crop(boxes[i])) for i in line] for line in _group_lines(boxes)
        ]
        return combine_lines(lines)


def ocr_for(language: str) -> MangaOcr | PpOcr:
    """The OCR engine for a source language code ('ja', 'en' or 'ko')."""
    if language == "ja":
        return MangaOcr()
    if language in ("en", "ko"):
        return PpOcr(language)  # type: ignore[arg-type]
    raise ValueError(f"no OCR engine for language {language!r}")
