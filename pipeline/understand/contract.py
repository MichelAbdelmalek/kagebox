"""The scene contract: what the vision model may say, and how it becomes a cited SceneJSON.

The model sees one panel and returns a ``PanelReading``. It never writes IDs, panel references
or citations; those are added here, by code. A claim is either read off the picture (visual) or
backed by an exact quote of the panel's own OCR text. A quote that is not in that text is
dropped and the field stays null, with an Issue for the review queue.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, model_validator

from pipeline.common.schemas import Cited, Provenance, SceneJSON, Strict
from pipeline.common.schemas.understanding import Figure, ShotHint

# Review item kinds raised by the understand stage.
REVIEW_KINDS: tuple[str, ...] = ("scene_failed", "scene_unsupported_quote")

MIN_QUOTE_CHARS = 2  # after normalisation; a single character proves nothing

_Text500 = Annotated[str, Field(max_length=500)]


class Claim(Strict):
    """One statement about the panel, with the kind of evidence behind it."""

    value: _Text500 | None = None
    evidence: Literal["visual", "text"] | None = None
    quote: _Text500 | None = None

    @model_validator(mode="after")
    def _evidence_rules(self) -> Claim:
        if self.value is None:
            if self.evidence is not None or self.quote is not None:
                raise ValueError("a null value carries no evidence and no quote")
        elif self.evidence is None:
            raise ValueError("a value needs evidence: visual or text")
        elif self.evidence == "text" and self.quote is None:
            raise ValueError("text evidence needs a quote")
        return self


class FigureReading(Strict):
    description: Claim
    emotion: Claim


class PanelReading(Strict):
    """The model's whole answer for one panel. No IDs, panel or citation fields."""

    setting: Claim
    action: Claim
    time_of_day: Claim
    weather: Claim
    shot_hint: ShotHint | None = None
    figures: list[FigureReading] = Field(default_factory=list, max_length=16)


class Issue(Strict):
    kind: Literal["scene_unsupported_quote"]
    field: str  # for example "weather" or "figures[1].emotion" (index in the model's answer)


_PUNCT = str.maketrans(
    {
        "\u3002": ".",
        "\u3001": ",",
        "\u300c": '"',
        "\u300d": '"',
        "\u300e": '"',
        "\u300f": '"',
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u301c": "~",
        "\uff5e": "~",
    }
)


def normalise(text: str) -> str:
    """NFKC, all whitespace removed, ASCII and full-width punctuation unified."""
    folded = unicodedata.normalize("NFKC", text).translate(_PUNCT)
    return re.sub(r"\s+", "", folded)


def _resolve(
    claim: Claim, field: str, panel_id: str, ocr_norm: str, issues: list[Issue]
) -> Cited[str]:
    """Turn a claim into a cited value, or an empty one (with an Issue if a quote failed)."""
    value = claim.value
    if value is None or not value.strip():
        return Cited[str]()
    if claim.evidence == "text":
        quote = normalise(claim.quote or "")
        if len(quote) < MIN_QUOTE_CHARS or quote not in ocr_norm:
            issues.append(Issue(kind="scene_unsupported_quote", field=field))
            return Cited[str]()
    return Cited[str](value=value, panel_ids=[panel_id])


def to_scene(
    reading: PanelReading,
    *,
    scene_id: str,
    panel_id: str,
    ocr_text: str,
    provenance: Provenance,
) -> tuple[SceneJSON, list[Issue]]:
    """Cite every kept claim to ``panel_id``; null every claim the evidence does not support."""
    issues: list[Issue] = []
    ocr_norm = normalise(ocr_text)

    def resolve(claim: Claim, field: str) -> Cited[str]:
        return _resolve(claim, field, panel_id, ocr_norm, issues)

    figures: list[Figure] = []
    for i, fig in enumerate(reading.figures):
        description = resolve(fig.description, f"figures[{i}].description")
        emotion = resolve(fig.emotion, f"figures[{i}].emotion")
        if description.known:
            figures.append(Figure(description=description, emotion=emotion))

    shot_hint: Cited[ShotHint] = (
        Cited[ShotHint]()
        if reading.shot_hint is None
        else Cited[ShotHint](value=reading.shot_hint, panel_ids=[panel_id])
    )
    scene = SceneJSON(
        id=scene_id,
        provenance=provenance,
        panel_id=panel_id,
        setting=resolve(reading.setting, "setting"),
        action=resolve(reading.action, "action"),
        shot_hint=shot_hint,
        time_of_day=resolve(reading.time_of_day, "time_of_day"),
        weather=resolve(reading.weather, "weather"),
        figures=figures,
    )
    return scene, issues


def schema_sha256() -> str:
    """Hash of the reading schema; part of the params hash so a schema change busts the cache."""
    canonical = json.dumps(
        PanelReading.model_json_schema(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
