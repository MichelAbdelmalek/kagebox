"""Per-panel scene understanding and the series-bible character record."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, computed_field, model_validator

from pipeline.common.ids import CharacterId, ObjectRef, PanelId
from pipeline.common.schemas.base import Artifact, Cited, Score, ShortText, Strict

ShotHint = Literal[
    "establishing_wide", "wide", "medium", "close_up", "extreme_close_up", "two_shot", "insert"
]

_CITED_FIELDS = ("setting", "action", "shot_hint", "time_of_day", "weather")


class Figure(Strict):
    """One figure seen in a panel. Identity (which character) is milestone 1.3's job."""

    description: Cited[ShortText]
    emotion: Cited[ShortText]


class SceneJSON(Artifact):
    """What a VLM read from one panel. Every known field must cite this panel."""

    panel_id: PanelId
    setting: Cited[ShortText]
    action: Cited[ShortText]
    shot_hint: Cited[ShotHint]
    time_of_day: Cited[ShortText]
    weather: Cited[ShortText]
    characters_present: list[Cited[CharacterId]] = Field(default_factory=list, max_length=32)
    emotion_by_character: dict[CharacterId, Cited[ShortText]] = Field(default_factory=dict)
    figures: list[Figure] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _cites_own_panel(self) -> SceneJSON:
        cited: list[tuple[str, Any]] = [(n, getattr(self, n)) for n in _CITED_FIELDS]
        cited += [(f"characters_present[{i}]", c) for i, c in enumerate(self.characters_present)]
        cited += [(f"emotion[{k}]", v) for k, v in self.emotion_by_character.items()]
        for i, f in enumerate(self.figures):
            cited += [(f"figures[{i}].description", f.description)]
            cited += [(f"figures[{i}].emotion", f.emotion)]
        for name, c in cited:
            if c.known and self.panel_id not in c.panel_ids:
                raise ValueError(f"{name} must cite its own panel {self.panel_id}")
        present = {c.value for c in self.characters_present if c.known}
        unknown = set(self.emotion_by_character) - present
        if unknown:
            raise ValueError(f"emotion given for characters not present: {sorted(unknown)}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unknown_fields(self) -> list[str]:
        """Fields left empty for lack of visual evidence (shown in the Panel reader)."""
        return [n for n in _CITED_FIELDS if not getattr(self, n).known]


class ReferenceCrop(Strict):
    panel_id: PanelId
    crop_ref: ObjectRef
    state: Literal["approved", "rejected", "pending"] = "pending"
    embedding_id: Annotated[str, Field(max_length=80)] | None = None


class CharacterRef(Artifact):
    id: CharacterId
    name: Cited[ShortText]
    crops: list[ReferenceCrop] = Field(default_factory=list, max_length=2000)
    outfits_by_chapter: dict[int, Cited[ShortText]] = Field(default_factory=dict)
    traits: list[Cited[ShortText]] = Field(default_factory=list, max_length=64)
    identity_threshold: Score = 0.90

    @computed_field  # type: ignore[prop-decorator]
    @property
    def approved_crop_count(self) -> int:
        return sum(1 for c in self.crops if c.state == "approved")
