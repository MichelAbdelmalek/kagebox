"""Prefixed, sortable identifiers (``panel_01j…``) and their validated string types."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints
from ulid import ULID

_B = r"_[0-9a-z_]{1,64}$"

ProjectId = Annotated[str, StringConstraints(pattern=r"^proj" + _B)]
ChapterId = Annotated[str, StringConstraints(pattern=r"^chap" + _B)]
PageId = Annotated[str, StringConstraints(pattern=r"^page" + _B)]
PanelId = Annotated[str, StringConstraints(pattern=r"^panel" + _B)]
BubbleId = Annotated[str, StringConstraints(pattern=r"^bubble" + _B)]
CharacterId = Annotated[str, StringConstraints(pattern=r"^char" + _B)]
ShotId = Annotated[str, StringConstraints(pattern=r"^shot" + _B)]
VoiceId = Annotated[str, StringConstraints(pattern=r"^v" + _B)]
VoiceLineId = Annotated[str, StringConstraints(pattern=r"^line" + _B)]
RightsId = Annotated[str, StringConstraints(pattern=r"^rights" + _B)]
UserId = Annotated[str, StringConstraints(pattern=r"^user" + _B)]
ArtifactId = Annotated[str, StringConstraints(pattern=r"^[a-z]{1,8}" + _B)]

# Object-store reference: "<bucket>/sha256/<hex>[.ext]". Never a URL or a filesystem path.
ObjectRef = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9-]{3,63}/sha256/[0-9a-f]{64}(\.[a-z0-9]{1,8})?$")
]

PREFIXES = frozenset(
    {"proj", "chap", "page", "panel", "bubble", "char", "shot", "v", "line", "rights", "user"}
)


def new_id(prefix: str) -> str:
    """Return a new sortable id such as ``panel_01jd3…``."""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")
    return f"{prefix}_{str(ULID()).lower()}"
