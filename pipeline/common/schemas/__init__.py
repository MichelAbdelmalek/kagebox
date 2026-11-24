"""Strict Pydantic v2 schemas for every inter-stage artifact."""

from pipeline.common.schemas.base import Artifact, Cited, ModelRef, Provenance, Strict
from pipeline.common.schemas.production import (
    Casting,
    Check,
    Shot,
    ShotLayer,
    StyleProfile,
    ValidationReport,
    Voice,
    VoiceLine,
    VoiceLocked,
    require_voice_usable,
)
from pipeline.common.schemas.rights import (
    RightsDenied,
    RightsRecord,
    require_cloud_allowed,
    require_render_allowed,
)
from pipeline.common.schemas.source import BBox, Bubble, Chapter, Page, Panel, Project
from pipeline.common.schemas.understanding import CharacterRef, Figure, ReferenceCrop, SceneJSON

__all__ = [
    "Artifact",
    "BBox",
    "Bubble",
    "Casting",
    "Chapter",
    "CharacterRef",
    "Check",
    "Cited",
    "Figure",
    "ModelRef",
    "Page",
    "Panel",
    "Project",
    "Provenance",
    "ReferenceCrop",
    "RightsDenied",
    "RightsRecord",
    "SceneJSON",
    "Shot",
    "ShotLayer",
    "Strict",
    "StyleProfile",
    "ValidationReport",
    "Voice",
    "VoiceLine",
    "VoiceLocked",
    "require_cloud_allowed",
    "require_render_allowed",
    "require_voice_usable",
]
