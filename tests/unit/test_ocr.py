"""OCR confidence, text sanitising and language routing, without loading any model."""

from __future__ import annotations

import math

import pytest

from pipeline.common.schemas.source import OCR_CONFIDENCE_MIN
from pipeline.ingest.ocr import (
    MAX_TEXT_LEN,
    MangaOcr,
    PpOcr,
    combine_lines,
    ocr_for,
    ocr_result,
    sanitize,
    token_confidence,
)


def test_token_confidence_mean() -> None:
    assert token_confidence([0.5, 1.0]) == pytest.approx(0.75)
    assert token_confidence([0.92]) == pytest.approx(0.92)
    assert token_confidence(()) == 0.0


def test_token_confidence_rejects_non_probabilities() -> None:
    for bad in ([1.2], [-0.1], [math.nan]):
        with pytest.raises(ValueError):
            token_confidence(bad)


def test_empty_text_has_zero_confidence_and_needs_review() -> None:
    # high per-token probabilities, but nothing readable came out (e.g. only EOS / whitespace)
    for text in ("", "   ", "\n\t", "\x00\x07"):
        got, conf = ocr_result(text, [0.99, 0.98])
        assert got == ""
        assert conf == 0.0
        assert conf < OCR_CONFIDENCE_MIN


def test_ocr_result_keeps_text_and_mean_confidence() -> None:
    assert ocr_result("HELLO", [0.9, 1.0]) == ("HELLO", pytest.approx(0.95))


def test_sanitize_strips_control_chars_keeps_newline_and_caps_length() -> None:
    assert sanitize("a\x00b\x1bc​d\ne\rf\tg\x7f") == "abcd\nefg"
    assert sanitize("  hi  \n") == "hi"
    assert len(sanitize("あ" * 5000)) == MAX_TEXT_LEN == 2000


def test_combine_lines_weights_confidence_per_character() -> None:
    # 4 chars at 1.0 and 1 char at 0.5 -> per-character mean 0.9, not the per-line mean 0.75
    text, conf = combine_lines([("ABCD", 1.0), ("E", 0.5)])
    assert text == "ABCD\nE"
    assert conf == pytest.approx(0.9)


def test_combine_lines_skips_empty_lines_and_empty_is_zero() -> None:
    assert combine_lines([("", math.nan), ("OK", 0.8)]) == ("OK", pytest.approx(0.8))
    assert combine_lines([]) == ("", 0.0)
    assert combine_lines([("", math.nan)]) == ("", 0.0)


def test_ocr_for_language_routing() -> None:
    ja = ocr_for("ja")
    assert isinstance(ja, MangaOcr)
    en, ko = ocr_for("en"), ocr_for("ko")
    assert isinstance(en, PpOcr) and en.lang == "en"
    assert isinstance(ko, PpOcr) and ko.lang == "ko"
    for unsupported in ("fr", "zh", "", "EN", "../en"):
        with pytest.raises(ValueError):
            ocr_for(unsupported)


def test_pp_ocr_rejects_unknown_language() -> None:
    with pytest.raises(ValueError):
        PpOcr("fr")  # type: ignore[arg-type]
