"""Director shots, voices, casting, voice lines, style profiles and validation reports."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, computed_field, model_validator

from pipeline.common.ids import (
    ArtifactId,
    BubbleId,
    CharacterId,
    ObjectRef,
    PanelId,
    ProjectId,
    ShotId,
    UserId,
    VoiceId,
    VoiceLineId,
)
from pipeline.common.schemas.base import Artifact, Cited, Language, Score, ShortText, Strict

# --- Director ---------------------------------------------------------------------------------

CameraMove = Literal["static", "pan", "tilt", "zoom_in", "zoom_out", "parallax_push"]
Transition = Literal["cut", "dissolve", "fade_black", "wipe"]


class ShotLayer(Strict):
    ref: ObjectRef
    depth: Annotated[int, Field(ge=0, le=16)]  # 0 = background
    parallax: Annotated[float, Field(ge=0.0, le=2.0)]


class Shot(Artifact):
    id: ShotId
    source_panel_ids: list[PanelId] = Field(min_length=1, max_length=16)
    keyframe_refs: list[ObjectRef] = Field(default_factory=list, max_length=16)
    camera_move: CameraMove
    duration_ms: Annotated[int, Field(ge=250, le=60_000)]
    transition: Transition = "cut"
    layers: list[ShotLayer] = Field(default_factory=list, max_length=17)
    dialogue_line_ids: list[VoiceLineId] = Field(default_factory=list, max_length=64)


# --- Voices and casting -----------------------------------------------------------------------

VoiceSource = Literal["designed", "licensed_actor", "cloned"]
ConsentStatus = Literal["not_required", "pending", "signed", "revoked"]


class Voice(Strict):
    id: VoiceId
    name: ShortText
    description: ShortText
    source: VoiceSource
    consent_status: ConsentStatus
    consent_doc_ref: ObjectRef | None = None
    engine: ShortText
    reference_audio_ref: ObjectRef | None = None
    languages: list[Language] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _consent_rules(self) -> Voice:
        if self.source == "designed":
            if self.consent_status != "not_required":
                raise ValueError("a designed voice has consent_status 'not_required'")
        else:
            if self.consent_status == "not_required":
                raise ValueError("a voice from a real person always needs consent")
            if self.consent_status == "signed" and self.consent_doc_ref is None:
                raise ValueError("signed consent needs a stored consent document")
            if self.reference_audio_ref is None and self.source == "cloned":
                raise ValueError("a cloned voice needs its reference audio")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def usable(self) -> bool:
        """Only designed voices or voices with signed consent can be used."""
        return self.source == "designed" or self.consent_status == "signed"


class VoiceLocked(PermissionError):
    """Raised when a voice without signed consent is used."""


def require_voice_usable(voice: Voice) -> Voice:
    if not voice.usable:
        raise VoiceLocked(f"voice {voice.id} is locked: consent is {voice.consent_status}")
    return voice


class Casting(Strict):
    project_id: ProjectId
    character_id: CharacterId
    language: Language
    voice_id: VoiceId
    version: Annotated[int, Field(ge=1)]
    changed_by: UserId
    changed_at: datetime


class VoiceLine(Artifact):
    id: VoiceLineId
    bubble_id: BubbleId
    panel_id: PanelId
    character_id: CharacterId
    language: Language
    text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    emotion: Cited[ShortText]
    voice_id: VoiceId
    casting_version: Annotated[int, Field(ge=1)]
    audio_ref: ObjectRef | None = None
    watermarked: bool = False
    wer: Annotated[float, Field(ge=0.0)] | None = None
    status: Literal["pending", "voiced", "passed", "review"] = "pending"

    @model_validator(mode="after")
    def _audio_is_watermarked(self) -> VoiceLine:
        if self.audio_ref is not None and not self.watermarked:
            raise ValueError("generated speech must be watermarked")
        return self


# --- Art direction ----------------------------------------------------------------------------

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-F]{6}$")]


class StyleProfile(Strict):
    project_id: ProjectId
    frame_timing: Literal["ones", "twos", "threes", "mixed"] = "mixed"
    shading: Literal["flat", "cel2", "screentone"] = "cel2"
    camera: Literal["multiplane", "free3d"] = "multiplane"
    lock_line_art: bool = True
    smear_frames: bool = True
    interpolation_scope: Literal["camera_only", "all", "none"] = "camera_only"
    negative_prompts: list[ShortText] = Field(
        default_factory=lambda: [
            "3D render",
            "CGI",
            "photorealistic",
            "plastic skin",
            "volumetric light",
            "depth of field",
            "motion blur",
        ],
        max_length=64,
    )
    style_threshold: Score = 0.85
    palette: list[HexColor] = Field(default_factory=list, max_length=64)


# --- Validation -------------------------------------------------------------------------------


class Check(Strict):
    name: Literal["whisper_wer", "identity", "style", "lip_sync", "traceability", "watermark"]
    metric: ShortText
    value: float | None  # None: the check could not run; that never counts as a pass
    threshold: float
    higher_is_better: bool
    evidence_refs: list[ObjectRef] = Field(default_factory=list, max_length=32)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        if self.value is None:
            return False
        return (
            self.value >= self.threshold
            if self.higher_is_better
            else (self.value <= self.threshold)
        )


class ValidationReport(Artifact):
    artifact_id: ArtifactId
    checks: list[Check] = Field(min_length=1, max_length=32)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> Literal["pass", "review"]:
        return "pass" if all(c.passed for c in self.checks) else "review"
