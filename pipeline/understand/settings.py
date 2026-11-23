"""Understand-stage settings (configs/understand.yaml) and the versioned prompt file."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from pipeline.common.dbmodels import STALE_INGEST_S
from pipeline.common.schemas import Strict
from pipeline.providers.registry import CONFIG_DIR, load_yaml

__all__ = ["UnderstandSettings", "load_settings", "prompt_text"]

# A slow panel must never look dead: all attempts of one panel stay under the stale limit.
MAX_PANEL_S = STALE_INGEST_S - 60

_VERSION = re.compile(r"^[A-Za-z0-9_.-]{1,16}$")


class UnderstandSettings(Strict):
    prompt_version: str
    threads: Annotated[int, Field(ge=1, le=16)]
    dtype: Literal["bfloat16", "float32"]
    max_pixels: Annotated[int, Field(ge=50_000, le=2_000_000)]
    max_new_tokens: Annotated[int, Field(ge=64, le=2048)]
    max_attempts: Annotated[int, Field(ge=1, le=5)]
    max_time_s: Annotated[int, Field(ge=10, le=300)]
    max_minutes_per_chapter: Annotated[int, Field(ge=1, le=1440)]
    # recall-oriented detector threshold for the text mask (the ingest default is 0.5)
    mask_detect_threshold: Annotated[float, Field(ge=0.1, le=0.9)]

    @model_validator(mode="after")
    def _limits(self) -> UnderstandSettings:
        if not _VERSION.fullmatch(self.prompt_version) or ".." in self.prompt_version:
            raise ValueError("prompt_version must be a short plain token")
        if self.max_attempts * self.max_time_s >= MAX_PANEL_S:
            raise ValueError(f"max_attempts * max_time_s must stay under {MAX_PANEL_S} s")
        return self


def load_settings(config_dir: Path = CONFIG_DIR) -> UnderstandSettings:
    return UnderstandSettings(**load_yaml("understand.yaml", config_dir))


def prompt_text(settings: UnderstandSettings, config_dir: Path = CONFIG_DIR) -> tuple[str, str]:
    """(text, sha256 of the file bytes) of configs/prompts/understand.<version>.md."""
    raw = (config_dir / "prompts" / f"understand.{settings.prompt_version}.md").read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()
