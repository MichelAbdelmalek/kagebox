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
import json
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import yaml
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


def validate_content(path: Path, fmt: str) -> None:
    """Format-aware content check, run after the hash matches and before a file is used.

    Safetensors are parsed structurally (a header-length byte of 0x80 is valid, not a pickle).
    Text formats must decode and parse. Opaque binary formats get the pickle sniff.
    """
    if fmt == "safetensors":
        size = path.stat().st_size
        with path.open("rb") as fh:
            raw = fh.read(8)
            n = int.from_bytes(raw, "little") if len(raw) == 8 else 0
            if not 2 <= n <= min(size - 8, 100 * 1024**2):
                raise UnsafeWeights(f"{path.name}: not a valid safetensors header")
            header = fh.read(n)
        try:
            if not header.startswith(b"{") or not isinstance(json.loads(header), dict):
                raise ValueError
        except ValueError as exc:
            raise UnsafeWeights(f"{path.name}: not a valid safetensors header") from exc
        return
    if fmt in ("json", "yaml", "text"):
        try:
            text = path.read_text(encoding="utf-8")
            if fmt == "json":
                json.loads(text)
            elif fmt == "yaml":
                yaml.safe_load(text)
        except (UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
            raise UnsafeWeights(f"{path.name}: not valid {fmt}") from exc
        return
    with path.open("rb") as fh:
        head = fh.read(8)
    if looks_like_pickle(head, path):
        raise UnsafeWeights(f"{path.name}: file content is a pickle")


def verify_file(path: Path, spec: FileSpec) -> Path:
    """Check a file on disk against its pinned spec. Used after download and before loading."""
    if path.stat().st_size != spec.size_bytes:
        raise UnsafeWeights(f"{spec.path}: size mismatch")
    if sha256_file(path) != spec.sha256:
        raise UnsafeWeights(f"{spec.path}: checksum mismatch")
    validate_content(path, spec.format)
    return path


# --- Offline conversions ------------------------------------------------------------------------
# Pickle checkpoints are allowed ONLY as conversion sources: fetched into a quarantine volume that
# runtime workers never mount, and read only by the network-less models-convert job.


class SourceFile(_Strict):
    path: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=200 * 1024**3)]
    format: Literal["torch-pickle", "safetensors"]

    _safe = field_validator("path")(FileSpec._safe_relative.__func__)  # type: ignore[attr-defined]


class ConversionStep(_Strict):
    src: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    dst: Annotated[str, StringConstraints(pattern=r"^[\w./-]+\.safetensors$", max_length=300)]
    key: Annotated[str, StringConstraints(max_length=100)] | None = None

    _safe_src = field_validator("src")(FileSpec._safe_relative.__func__)  # type: ignore[attr-defined]
    _safe_dst = field_validator("dst")(FileSpec._safe_relative.__func__)  # type: ignore[attr-defined]


class Reference(_Strict):
    """An independent safetensors copy the converted tensors must equal (e.g. an official one)."""

    step_dst: str
    repo: RepoId
    revision: CommitSha
    path: Annotated[str, StringConstraints(pattern=r"^[\w./-]+\.safetensors$", max_length=300)]
    sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0)]


class Conversion(_Strict):
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    status: Literal["approved", "proposed"]
    repo: RepoId
    revision: CommitSha
    license: Annotated[str, StringConstraints(min_length=2, max_length=100)]
    source: Annotated[str, StringConstraints(pattern=r"^https://", max_length=300)]
    sources: list[SourceFile] = Field(max_length=50)
    steps: list[ConversionStep] = Field(min_length=1, max_length=50)
    data_files: list[FileSpec] = Field(default_factory=list, max_length=100)
    # Output sha256 per step dst; null until the first conversion has been reviewed and pinned.
    outputs: dict[str, Sha256 | None]
    reference: Reference | None = None
    # Extra tensor name -> reference tensor it must equal (tied weights a converter dropped).
    tied: dict[
        Annotated[str, StringConstraints(max_length=200)],
        Annotated[str, StringConstraints(max_length=200)],
    ] = Field(default_factory=dict)
    safe_globals: list[Annotated[str, StringConstraints(pattern=r"^[\w.]+$")]] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def _consistent(self) -> Conversion:
        pinned = {s.path for s in self.sources}
        for step in self.steps:
            if step.src not in pinned:
                raise ValueError(f"{self.name}: step source {step.src} is not a pinned source")
        if set(self.outputs) != {s.dst for s in self.steps}:
            raise ValueError(f"{self.name}: outputs must list exactly the step destinations")
        if self.reference and self.reference.step_dst not in self.outputs:
            raise ValueError(f"{self.name}: reference names an unknown step")
        if self.tied and not self.reference:
            raise ValueError(f"{self.name}: `tied` only applies to a reference comparison")
        for f in self.data_files:
            if f.format not in ("json", "text", "sentencepiece", "tiktoken", "yaml"):
                raise ValueError(f"{self.name}: data file {f.path} must be a data format")
        return self


def load_conversions(raw: dict[str, Any]) -> list[Conversion]:
    items = raw.get("conversions") or {}
    if not isinstance(items, dict):
        raise ValueError("models.yaml: `conversions` must be a mapping")
    return [Conversion.model_validate({"name": k, **v}) for k, v in items.items()]
