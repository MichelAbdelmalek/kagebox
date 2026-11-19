"""Shared building blocks: strict model config, provenance and cited (evidence-backed) values."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pipeline.common.ids import ArtifactId, PanelId

SCHEMA_VERSION: Literal[1] = 1

Stage = Literal[
    "ingest", "understand", "bible", "director", "render", "audio", "validate", "assemble"
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
Language = Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")]
Score = Annotated[float, Field(ge=0.0, le=1.0)]


class Strict(BaseModel):
    """Base for every inter-stage artifact: strict types, no unknown keys, immutable."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


Revision = Annotated[str, StringConstraints(max_length=80)]
HttpsUrl = Annotated[str, StringConstraints(pattern=r"^https://", max_length=500)]


class ModelRef(Strict):
    """One pinned model that contributed to an artifact (from configs/models.yaml)."""

    name: ShortText
    revision: Revision
    license: ShortText
    source_url: HttpsUrl | None = None


class Provenance(Strict):
    """Where an artifact came from. Stored as a JSON manifest next to the artifact.

    ``tool`` is the main tool; ``components`` lists every model that took part (for example the
    bubble detector and each OCR model behind a Bubble).
    """

    stage: Stage
    tool: ShortText  # model name, or the program (e.g. "ffmpeg") for non-model steps
    tool_version: ShortText
    model_revision: Revision | None = None
    license: ShortText
    source_url: HttpsUrl | None = None
    provider: Literal["together", "local", "none"]
    params_hash: Sha256
    input_hashes: list[Sha256] = Field(default_factory=list)
    components: list[ModelRef] = Field(default_factory=list, max_length=16)
    created_at: datetime


class Artifact(Strict):
    id: ArtifactId
    schema_version: Literal[1] = SCHEMA_VERSION
    provenance: Provenance


T = TypeVar("T")


class Cited(Strict, Generic[T]):
    """A value backed by evidence from source panels.

    ``value is None`` means there was no visual evidence; that is a valid, expected state.
    A non-null value must cite at least one panel. Nothing is ever guessed.
    """

    value: T | None = None
    panel_ids: list[PanelId] = Field(default_factory=list, max_length=64)
    confidence: Score | None = None

    @model_validator(mode="after")
    def _evidence_required(self) -> Cited[T]:
        if self.value is not None and not self.panel_ids:
            raise ValueError("a non-null value must cite at least one source panel id")
        if self.value is None and self.confidence is not None:
            raise ValueError("an empty (no-evidence) value cannot carry a confidence")
        return self

    @property
    def known(self) -> bool:
        return self.value is not None
