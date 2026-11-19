"""Local model manifests and the weights policy.

Policy:
- Every file is pinned by repo + 40-char commit SHA + sha256 + size.
- Pickle-based formats (.pt, .pth, .ckpt, .pkl, PyTorch .bin) are never accepted. Files are
  also sniffed, so a pickle renamed to an allowed suffix is still refused.
- `.safetensors` is the default weights format. Other non-pickle formats (ONNX, CTranslate2,
  Paddle inference) are allowed only for a model whose manifest carries a reviewed, justified
  `format_exception` naming that format, in the same spirit as the trust_remote_code allowlist.
- Small data files a model needs (configs, tokenizers, vocabularies) are allowed as data.
"""

from __future__ import annotations

import hashlib
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

WeightFormat = Literal["safetensors", "onnx", "ctranslate2", "paddle"]
DataFormat = Literal["json", "text", "sentencepiece", "tiktoken", "yaml"]
FileFormat = WeightFormat | DataFormat

GATED_WEIGHT_FORMATS: frozenset[str] = frozenset({"onnx", "ctranslate2", "paddle"})
PICKLE_SUFFIXES = frozenset({".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".joblib", ".npy", ".npz"})
SUFFIXES: dict[str, frozenset[str]] = {
    "safetensors": frozenset({".safetensors"}),
    "onnx": frozenset({".onnx", ".data"}),  # .data = ONNX external tensor data
    "ctranslate2": frozenset({".bin"}),  # CTranslate2's own flat format, not torch pickle
    "paddle": frozenset({".pdmodel", ".pdiparams", ".json"}),
    "json": frozenset({".json"}),
    "text": frozenset({".txt", ".md"}),
    "sentencepiece": frozenset({".model"}),
    "tiktoken": frozenset({".tiktoken"}),
    "yaml": frozenset({".yaml", ".yml"}),
}

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
RepoId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][\w.-]{0,95}/[\w.-]{1,96}$")]


class UnsafeWeights(PermissionError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class FileSpec(_Strict):
    path: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=200 * 1024**3)]
    format: FileFormat

    @field_validator("path")
    @classmethod
    def _safe_relative(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or "\\" in v or "\x00" in v or v.startswith("."):
            raise ValueError(f"unsafe file path {v!r}")
        return v

    @model_validator(mode="after")
    def _suffix_matches_format(self) -> FileSpec:
        suffix = PurePosixPath(self.path).suffix.lower()
        if suffix in PICKLE_SUFFIXES:
            raise ValueError(f"{self.path}: pickle-based formats are never allowed")
        if suffix not in SUFFIXES[self.format]:
            raise ValueError(f"{self.path}: suffix {suffix!r} does not match format {self.format}")
        return self


class FormatException(_Strict):
    formats: list[Literal["onnx", "ctranslate2", "paddle"]] = Field(min_length=1)
    justification: Annotated[str, StringConstraints(min_length=20, max_length=1000)]
    reviewed_by: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    reviewed_at: Annotated[date, Field(strict=False)]  # YAML may give a date or an ISO string


class LocalModel(_Strict):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    status: Literal["approved", "proposed"]
    repo: RepoId
    revision: CommitSha
    license: Annotated[str, StringConstraints(min_length=2, max_length=100)]
    source: Annotated[str, StringConstraints(pattern=r"^https://", max_length=300)]
    files: list[FileSpec] = Field(min_length=1, max_length=200)
    format_exception: FormatException | None = None

    @model_validator(mode="after")
    def _gated_formats_need_exception(self) -> LocalModel:
        allowed = set(self.format_exception.formats) if self.format_exception else set()
        for f in self.files:
            if f.format in GATED_WEIGHT_FORMATS and f.format not in allowed:
                raise ValueError(
                    f"{self.name}: {f.path} is {f.format}; add a reviewed format_exception"
                )
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError(f"{self.name}: duplicate file paths")
        return self


def looks_like_pickle(head: bytes, path: Path | None = None) -> bool:
    """Detect raw pickles (protocol 2-5) and PyTorch zip checkpoints regardless of suffix."""
    if len(head) >= 2 and head[0] == 0x80 and 2 <= head[1] <= 5:
        return True
    if head[:4] == b"PK\x03\x04" and path is not None:
        try:
            with zipfile.ZipFile(path) as zf:
                return any(n.endswith("data.pkl") for n in zf.namelist())
        except zipfile.BadZipFile:
            return False
    return False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file(path: Path, spec: FileSpec) -> Path:
    """Check a file on disk against its pinned spec. Used after download and before loading."""
    if path.stat().st_size != spec.size_bytes:
        raise UnsafeWeights(f"{spec.path}: size mismatch")
    with path.open("rb") as fh:
        head = fh.read(8)
    if looks_like_pickle(head, path):
        raise UnsafeWeights(f"{spec.path}: file content is a pickle")
    if sha256_file(path) != spec.sha256:
        raise UnsafeWeights(f"{spec.path}: checksum mismatch")
    return path
