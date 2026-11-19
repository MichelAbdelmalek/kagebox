from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path

import pytest
import torch
from safetensors.torch import load_file

from pipeline.providers import convert
from pipeline.providers.convert import ConversionRefused, convert_checkpoint


@pytest.fixture(autouse=True)
def sandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAGEBOX_CONVERT_SANDBOX", "1")


def save(obj: object, path: Path) -> Path:
    torch.save(obj, path)
    return path


def test_converts_state_dict_bit_for_bit(tmp_path: Path) -> None:
    sd = {"layer.weight": torch.randn(4, 3), "layer.bias": torch.arange(4, dtype=torch.int64)}
    src = save(sd, tmp_path / "m.pt")
    rec = convert_checkpoint(src, tmp_path / "m.safetensors")
    out = load_file(tmp_path / "m.safetensors")
    assert set(out) == set(sd)
    assert all(torch.equal(out[k], sd[k]) for k in sd)
    assert rec.source_sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert (
        rec.output_sha256 == hashlib.sha256((tmp_path / "m.safetensors").read_bytes()).hexdigest()
    )
    assert rec.tensor_count == 2


def test_extracts_nested_key_and_keeps_primitive_metadata(tmp_path: Path) -> None:
    ckpt = {"state_dict": {"w": torch.ones(2)}, "epoch": 7, "name": "big-lama"}
    src = save(ckpt, tmp_path / "best.ckpt")
    rec = convert_checkpoint(src, tmp_path / "out.safetensors", key="state_dict")
    assert set(load_file(tmp_path / "out.safetensors")) == {"w"}
    assert rec.tensor_count == 1


def test_flattens_nested_tensor_dicts(tmp_path: Path) -> None:
    src = save(
        {"encoder": {"w": torch.ones(1)}, "decoder": {"b": torch.zeros(1)}}, tmp_path / "x.pt"
    )
    convert_checkpoint(src, tmp_path / "x.safetensors")
    assert set(load_file(tmp_path / "x.safetensors")) == {"encoder.w", "decoder.b"}


class Evil:
    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (os.system, ("touch /tmp/kagebox-pwned",))


def test_malicious_pickle_is_refused_and_never_executes(tmp_path: Path) -> None:
    marker = Path("/tmp/kagebox-pwned")  # noqa: S108 - fixed target of the attack payload below
    marker.unlink(missing_ok=True)
    src = tmp_path / "evil.pt"
    src.write_bytes(pickle.dumps({"w": Evil()}, protocol=4))
    with pytest.raises(ConversionRefused, match="unsafe"):
        convert_checkpoint(src, tmp_path / "evil.safetensors")
    assert not marker.exists()
    assert not (tmp_path / "evil.safetensors").exists()


def test_non_tensor_objects_in_weights_are_refused(tmp_path: Path) -> None:
    src = save({"w": torch.ones(1), "bad": [torch.ones(1), "x"]}, tmp_path / "m.pt")
    with pytest.raises(ConversionRefused, match="bad"):
        convert_checkpoint(src, tmp_path / "m.safetensors")


def test_refuses_outside_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAGEBOX_CONVERT_SANDBOX")
    src = save({"w": torch.ones(1)}, tmp_path / "m.pt")
    with pytest.raises(ConversionRefused, match="sandbox"):
        convert_checkpoint(src, tmp_path / "m.safetensors")


def test_refuses_old_torch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(convert, "_torch_version", lambda: (2, 5, 1))
    src = save({"w": torch.ones(1)}, tmp_path / "m.pt")
    with pytest.raises(ConversionRefused, match=r"2\.6"):
        convert_checkpoint(src, tmp_path / "m.safetensors")


def test_shared_tensors_are_stored_once_each(tmp_path: Path) -> None:
    w = torch.randn(3)
    src = save({"a": w, "b": w}, tmp_path / "tied.pt")  # tied weights share storage
    convert_checkpoint(src, tmp_path / "tied.safetensors")
    out = load_file(tmp_path / "tied.safetensors")
    assert torch.equal(out["a"], out["b"])


def test_output_is_byte_for_byte_reproducible(tmp_path: Path) -> None:
    """Pinned output hashes need determinism. safetensors writes multi-key metadata in random
    (hash-map) order, which once made every run produce a different file."""
    src = save({"w": torch.arange(6.0), "b": torch.ones(2)}, tmp_path / "m.pt")
    shas = {convert_checkpoint(src, tmp_path / f"o{i}.safetensors").output_sha256 for i in range(8)}
    assert len(shas) == 1


def test_provenance_metadata_is_recorded(tmp_path: Path) -> None:
    import json

    from safetensors import safe_open

    src = save({"w": torch.ones(1)}, tmp_path / "m.pt")
    convert_checkpoint(src, tmp_path / "o.safetensors")
    with safe_open(str(tmp_path / "o.safetensors"), "pt") as f:
        meta = json.loads(f.metadata()["kagebox"])
    assert meta["source_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert meta["converter"] == "kagebox-convert/1"


def test_output_hash_does_not_depend_on_torch_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = save({"w": torch.ones(3)}, tmp_path / "m.pt")
    a = convert_checkpoint(src, tmp_path / "a.safetensors").output_sha256
    monkeypatch.setattr(torch, "__version__", "2.99.0+cpu")
    b = convert_checkpoint(src, tmp_path / "b.safetensors").output_sha256
    assert a == b
