from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import torch
from pydantic import ValidationError
from safetensors.torch import save_file

from orchestration.models_convert import promote_outputs, run_conversion
from pipeline.providers.convert import ConversionRefused
from pipeline.providers.weights import Conversion

REV = "b" * 40


@pytest.fixture(autouse=True)
def sandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAGEBOX_CONVERT_SANDBOX", "1")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def spec(src: Path, **over: Any) -> Conversion:
    base: dict[str, Any] = {
        "name": "toy",
        "status": "approved",
        "repo": "org/toy",
        "revision": REV,
        "license": "Apache-2.0",
        "source": "https://huggingface.co/org/toy",
        "sources": [
            {
                "path": "pytorch_model.bin",
                "sha256": sha(src),
                "size_bytes": src.stat().st_size,
                "format": "torch-pickle",
            }
        ],
        "steps": [{"src": "pytorch_model.bin", "dst": "model.safetensors"}],
        "outputs": {"model.safetensors": None},
    }
    base.update(over)
    return Conversion.model_validate(base)


def layout(tmp_path: Path, tensors: dict[str, torch.Tensor]) -> tuple[Path, Path, Path]:
    quarantine, models = tmp_path / "q", tmp_path / "m"
    (quarantine / "toy" / REV).mkdir(parents=True)
    src = quarantine / "toy" / REV / "pytorch_model.bin"
    torch.save(tensors, src)
    return quarantine, models, src


def test_spec_only_allows_pickle_as_quarantined_source() -> None:
    with pytest.raises(ValidationError):
        Conversion.model_validate(
            {
                "name": "toy",
                "status": "proposed",
                "repo": "org/toy",
                "revision": REV,
                "license": "MIT",
                "source": "https://x",
                "sources": [],
                "steps": [{"src": "a.bin", "dst": "a.pt"}],
                "outputs": {"a.pt": None},
            }
        )


def test_step_must_reference_a_pinned_source(tmp_path: Path) -> None:
    _, _, src = layout(tmp_path, {"w": torch.ones(1)})
    with pytest.raises(ValidationError, match="not a pinned source"):
        spec(src, steps=[{"src": "other.bin", "dst": "model.safetensors"}])


def test_converts_into_models_dir_and_reports_output_hash(tmp_path: Path) -> None:
    q, m, src = layout(tmp_path, {"w": torch.ones(2, 2)})
    report = run_conversion(spec(src), q, m)
    out = m / "toy" / REV / "model.safetensors"
    assert out.exists() and report["model.safetensors"] == sha(out)


def test_pinned_output_hash_must_match(tmp_path: Path) -> None:
    q, m, src = layout(tmp_path, {"w": torch.ones(2)})
    with pytest.raises(ConversionRefused, match="output hash"):
        run_conversion(spec(src, outputs={"model.safetensors": "0" * 64}), q, m)
    assert not (m / "toy" / REV / "model.safetensors").exists()


def test_tampered_source_is_refused(tmp_path: Path) -> None:
    q, m, src = layout(tmp_path, {"w": torch.ones(2)})
    s = spec(src)
    src.write_bytes(src.read_bytes() + b"x")
    with pytest.raises(ConversionRefused, match="source"):
        run_conversion(s, q, m)


def test_matches_independent_reference(tmp_path: Path) -> None:
    tensors = {"w": torch.randn(3)}
    q, m, src = layout(tmp_path, tensors)
    ref = q / "toy" / REV / "_reference" / "reference.safetensors"
    ref.parent.mkdir()
    save_file(tensors, str(ref))
    s = spec(
        src,
        reference={
            "step_dst": "model.safetensors",
            "path": "reference.safetensors",
            "repo": "org/toy",
            "revision": "c" * 40,
            "sha256": sha(ref),
            "size_bytes": ref.stat().st_size,
        },
    )
    run_conversion(s, q, m)


def test_reference_mismatch_is_refused(tmp_path: Path) -> None:
    q, m, src = layout(tmp_path, {"w": torch.ones(3)})
    ref = q / "toy" / REV / "_reference" / "reference.safetensors"
    ref.parent.mkdir()
    save_file({"w": torch.zeros(3)}, str(ref))
    s = spec(
        src,
        reference={
            "step_dst": "model.safetensors",
            "path": "reference.safetensors",
            "repo": "org/toy",
            "revision": "c" * 40,
            "sha256": sha(ref),
            "size_bytes": ref.stat().st_size,
        },
    )
    with pytest.raises(ConversionRefused, match="reference"):
        run_conversion(s, q, m)
    assert not (m / "toy" / REV / "model.safetensors").exists()


@pytest.mark.parametrize("field", ["source", "step_src", "step_dst"])
def test_paths_cannot_escape(tmp_path: Path, field: str) -> None:
    _, _, src = layout(tmp_path, {"w": torch.ones(1)})
    over: dict[str, Any] = {}
    if field == "source":
        over["sources"] = [
            {"path": "../evil.bin", "sha256": sha(src), "size_bytes": 1, "format": "torch-pickle"}
        ]
        over["steps"] = [{"src": "../evil.bin", "dst": "model.safetensors"}]
    elif field == "step_src":
        over["sources"] = [
            {"path": "/etc/evil.bin", "sha256": sha(src), "size_bytes": 1, "format": "torch-pickle"}
        ]
        over["steps"] = [{"src": "/etc/evil.bin", "dst": "model.safetensors"}]
    else:
        over["steps"] = [{"src": "pytorch_model.bin", "dst": "../../model.safetensors"}]
        over["outputs"] = {"../../model.safetensors": None}
    with pytest.raises(ValidationError, match="unsafe"):
        spec(src, **over)


def _with_reference(
    tmp_path: Path, ours: dict[str, torch.Tensor], ref: dict[str, torch.Tensor]
) -> tuple[Conversion, Path, Path]:
    q, m, src = layout(tmp_path, ours)
    ref_path = q / "toy" / REV / "_reference" / "reference.safetensors"
    ref_path.parent.mkdir()
    save_file(ref, str(ref_path))
    s = spec(
        src,
        reference={
            "step_dst": "model.safetensors",
            "path": "reference.safetensors",
            "repo": "org/toy",
            "revision": "c" * 40,
            "sha256": sha(ref_path),
            "size_bytes": ref_path.stat().st_size,
        },
    )
    return s, q, m


def test_reference_without_tied_duplicate_is_accepted(tmp_path: Path) -> None:
    emb = torch.randn(4, 2)
    s, q, m = _with_reference(
        tmp_path, {"embed.weight": emb, "lm_head.weight": emb}, {"embed.weight": emb.clone()}
    )
    s = Conversion.model_validate({**s.model_dump(), "tied": {"lm_head.weight": "embed.weight"}})
    run_conversion(s, q, m)


def test_extra_tensor_that_is_not_a_duplicate_is_refused(tmp_path: Path) -> None:
    s, q, m = _with_reference(
        tmp_path, {"a": torch.ones(2), "b": torch.zeros(2) + 5}, {"a": torch.ones(2)}
    )
    with pytest.raises(ConversionRefused, match="reference"):
        run_conversion(s, q, m)


def test_tensor_missing_from_ours_is_refused(tmp_path: Path) -> None:
    s, q, m = _with_reference(
        tmp_path, {"a": torch.ones(2)}, {"a": torch.ones(2), "b": torch.ones(2)}
    )
    with pytest.raises(ConversionRefused, match="reference"):
        run_conversion(s, q, m)


def test_equal_valued_extra_tensor_is_refused_unless_declared_tied(tmp_path: Path) -> None:
    emb = torch.randn(4, 2)
    s, q, m = _with_reference(
        tmp_path, {"embed.weight": emb, "lm_head.weight": emb}, {"embed.weight": emb.clone()}
    )
    with pytest.raises(ConversionRefused, match="reference"):
        run_conversion(s, q, m)


# --- promotion from staging to the runtime models volume ------------------------------------


def _staged(tmp_path: Path, pinned: bool) -> tuple[Conversion, Path, Path]:
    q, staging, src = layout(tmp_path, {"w": torch.ones(2)})
    report = run_conversion(spec(src), q, staging)
    out = {"model.safetensors": report["model.safetensors"] if pinned else None}
    return spec(src, outputs=out), staging, tmp_path / "models"


def test_promote_copies_pinned_output_read_only(tmp_path: Path) -> None:
    c, staging, models = _staged(tmp_path, pinned=True)
    [path] = promote_outputs(c, staging, models)
    assert path == models / "toy" / REV / "model.safetensors"
    assert sha(path) == c.outputs["model.safetensors"]
    assert not path.stat().st_mode & 0o222


def test_promote_refuses_unpinned_output(tmp_path: Path) -> None:
    c, staging, models = _staged(tmp_path, pinned=False)
    with pytest.raises(ConversionRefused, match="not pinned"):
        promote_outputs(c, staging, models)
    assert not models.exists() or not any(models.rglob("*.safetensors"))


def test_promote_refuses_tampered_staging(tmp_path: Path) -> None:
    c, staging, models = _staged(tmp_path, pinned=True)
    f = staging / "toy" / REV / "model.safetensors"
    f.chmod(0o644)
    f.write_bytes(f.read_bytes()[:-1] + b" ")
    with pytest.raises(ConversionRefused, match="hash"):
        promote_outputs(c, staging, models)
    assert not (models / "toy" / REV / "model.safetensors").exists()
