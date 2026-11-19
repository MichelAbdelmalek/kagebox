"""OCR confidence, text sanitising and language routing, without loading any model."""

from __future__ import annotations

import math

import pytest
from PIL import Image

from pipeline.common.schemas.source import OCR_CONFIDENCE_MIN
from pipeline.ingest.ocr import (
    MAX_TEXT_LEN,
    REPLACEMENT,
    MangaOcr,
    PpOcr,
    combine_lines,
    ocr_for,
    ocr_result,
    order_boxes,
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
    text, conf = combine_lines([[("ABCD", 1.0)], [("E", 0.5)]])
    assert text == "ABCD\nE"
    assert conf == pytest.approx(0.9)


def test_combine_lines_joins_boxes_of_a_line_with_a_space() -> None:
    assert combine_lines([[("A", 1.0), ("B", 0.8)], [("C", 0.9)]]) == (
        "A B\nC",
        pytest.approx(0.9),
    )


def test_combine_lines_counts_an_empty_recognition_as_zero() -> None:
    # R2: a detected box that recognises to nothing is a 0.0 contribution with weight 1
    text, conf = combine_lines([[("", math.nan), ("OK", 0.8)], [(" ", 0.9)]])
    assert text == "OK"
    assert conf == pytest.approx((0.8 + 0.8 + 0.0 + 0.0) / 4)
    assert conf < OCR_CONFIDENCE_MIN
    assert combine_lines([]) == ("", 0.0)
    assert combine_lines([[("", math.nan)]]) == ("", 0.0)


def test_order_boxes_groups_lines_by_vertical_overlap() -> None:
    # the right word box sits 2 px higher than the left one: still the same line, read left first
    assert order_boxes([(50, 12, 90, 30), (0, 10, 40, 30), (0, 40, 40, 60)]) == [1, 0, 2]
    assert order_boxes([]) == []


def test_order_boxes_needs_half_the_smaller_height_to_share_a_line() -> None:
    # overlap 10 of the smaller height 20 (50 %) -> same line; 9 of 20 -> next line
    assert order_boxes([(100, 0, 140, 20), (0, 10, 40, 30)]) == [1, 0]
    assert order_boxes([(100, 0, 140, 20), (0, 11, 40, 31)]) == [0, 1]


def test_order_boxes_three_lines_top_to_bottom() -> None:
    boxes = [(0, 100, 30, 120), (40, 0, 80, 20), (0, 50, 30, 70), (0, 1, 30, 21)]
    assert order_boxes(boxes) == [3, 1, 2, 0]


class FakePp(PpOcr):
    """PpOcr with the detector and recogniser replaced by fixed outputs."""

    def __init__(self, boxes: list[tuple[int, int, int, int]], reads: dict[int, tuple[str, float]]):
        super().__init__("en")
        self.boxes, self.reads = boxes, reads

    def _detect(self, img: Image.Image) -> list[tuple[int, int, int, int]]:
        return self.boxes

    def _recognize(self, crop: Image.Image) -> tuple[str, float]:
        return self.reads[crop.width]  # each fake box has a distinct width


def test_pp_ocr_read_orders_and_combines_lines() -> None:
    # right word 1 px higher than the left one, then a second line
    boxes = [(60, 9, 72, 30), (0, 10, 11, 30), (0, 40, 13, 60)]
    reads = {12: ("B", 0.9), 11: ("A", 1.0), 13: ("C", 0.8)}
    text, conf = FakePp(boxes, reads).read(Image.new("RGB", (100, 100), "white"))
    assert text == "A B\nC"
    assert conf == pytest.approx(0.9)


def test_pp_ocr_read_flags_a_detected_line_that_reads_empty() -> None:
    boxes = [(0, 0, 11, 20), (0, 40, 12, 60)]
    reads = {11: ("HELLO", 1.0), 12: ("", math.nan)}
    text, conf = FakePp(boxes, reads).read(Image.new("RGB", (100, 100), "white"))
    assert text == "HELLO"
    assert conf == pytest.approx(5 / 6)
    assert conf < OCR_CONFIDENCE_MIN


def test_pp_ocr_read_nothing_detected_is_empty_and_zero() -> None:
    assert FakePp([], {}).read(Image.new("RGB", (10, 10), "white")) == ("", 0.0)


PAD, UNK, BOS, EOS, MASK = 0, 1, 2, 3, 4
VOCAB = {5: "こ", 6: "ん", 7: "に"}


class FakeTok:
    def __init__(self) -> None:
        self.pad_token_id, self.unk_token_id, self.mask_token_id = PAD, UNK, MASK
        self.cls_token_id, self.sep_token_id = BOS, EOS
        self.bos_token_id: int | None = None
        self.eos_token_id: int | None = None
        self.all_special_ids = [PAD, UNK, BOS, EOS, MASK]

    def decode(self, ids: list[int], skip_special_tokens: bool = False) -> str:
        assert all(i in VOCAB for i in ids), ids  # only text tokens are decoded
        return " ".join(VOCAB[i] for i in ids)  # char tokenizer: spaces between characters


class FakeManga(MangaOcr):
    def __init__(self, ids: list[int], probs: list[float]) -> None:
        super().__init__()
        self.ids, self.probs = ids, probs

    def _tokenizer(self) -> FakeTok:  # type: ignore[override]
        return FakeTok()

    def _generate(self, img: Image.Image) -> tuple[list[int], list[float]]:
        return self.ids, self.probs


def test_manga_ocr_excludes_bos_eos_pad_from_text_and_confidence() -> None:
    text, conf = FakeManga([5, 6, EOS, PAD], [0.9, 0.7, 0.1, 0.1]).read(Image.new("RGB", (8, 8)))
    assert text == "こん"
    assert conf == pytest.approx(0.8)


def test_manga_ocr_unk_becomes_replacement_char_and_zero_confidence() -> None:
    # R1: [UNK] is the model saying it could not read a character
    ids, probs = [5, UNK, 6, 7, EOS], [1.0, 0.99, 1.0, 1.0, 1.0]
    text, conf = FakeManga(ids, probs).read(Image.new("RGB", (8, 8)))
    assert text == f"こ{REPLACEMENT}んに"
    assert conf == pytest.approx(3 / 4)
    assert conf < OCR_CONFIDENCE_MIN


def test_manga_ocr_only_unk_is_flagged() -> None:
    text, conf = FakeManga([UNK, EOS], [0.99, 1.0]).read(Image.new("RGB", (8, 8)))
    assert text == REPLACEMENT
    assert conf == 0.0


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
