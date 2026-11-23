"""The scene contract: what the vision model may say, and how it becomes a cited SceneJSON.

The model sees one panel and returns a ``PanelReading``. It never writes IDs, panel references
or citations; those are added here, by code. Scene fields describe what is DRAWN: a claim whose
evidence is ``text`` (taken from the speech bubbles) is dropped and the field stays null, with an
Issue for the review queue. Dialogue is already stored as OCR bubbles, and text inside a panel is
untrusted data that must never steer a scene field.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field

from pipeline.common.schemas import Cited, Provenance, SceneJSON, Strict
from pipeline.common.schemas.understanding import Figure, ShotHint

# Review item kinds raised by the understand stage.
REVIEW_KINDS: tuple[str, ...] = ("scene_failed", "scene_text_claim")

_Text500 = Annotated[str, Field(max_length=500)]


class Claim(Strict):
    """One statement about the panel, with the kind of evidence behind it.

    Scene fields are visual-only, so a value without an evidence label counts as drawn (the 2B
    model often omits it), and an evidence label on a null value means nothing. Only ``text``
    evidence is refused, and the model's label is not a security boundary: the panel is sent
    with its text regions masked (``pipeline/understand/mask.py``).
    """

    value: _Text500 | None = None
    evidence: Literal["visual", "text"] | None = None


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
    kind: Literal["scene_text_claim"]
    field: str  # for example "weather" or "figures[1].emotion" (index in the model's answer)


def _resolve(claim: Claim, field: str, panel_id: str, issues: list[Issue]) -> Cited[str]:
    """Turn a claim into a cited value, or an empty one (with an Issue for text evidence)."""
    value = claim.value
    if value is None or not value.strip():
        return Cited[str]()
    if claim.evidence == "text":
        issues.append(Issue(kind="scene_text_claim", field=field))  # field name only, never text
        return Cited[str]()
    return Cited[str](value=value, panel_ids=[panel_id])


def to_scene(
    reading: PanelReading,
    *,
    scene_id: str,
    panel_id: str,
    provenance: Provenance,
) -> tuple[SceneJSON, list[Issue]]:
    """Cite every kept claim to ``panel_id``; null every claim that is not drawn."""
    issues: list[Issue] = []

    def resolve(claim: Claim, field: str) -> Cited[str]:
        return _resolve(claim, field, panel_id, issues)

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
