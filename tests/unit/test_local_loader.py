from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import torch

from pipeline.providers import local
from pipeline.providers.registry import ModelNotApproved
from pipeline.providers.weights import UnsafeWeights

REV = "c" * 40


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def raw_cfg(status: str = "approved", data: bytes = b'{"a": 1}') -> dict[str, Any]:
    return {
        "models": {
            "toy": {
                "status": status,
                "repo": "org/toy",
                "revision": REV,
                "license": "Apache-2.0",
                "source": "https://huggingface.co/org/toy",
                "files": [
                    {
                        "path": "config.json",
                        "sha256": sha(data),
                        "size_bytes": len(data),
                        "format": "json",
                    }
                ],
            }
        }
    }


def install(root: Path, data: bytes = b'{"a": 1}') -> Path:
    d = root / "toy" / REV
    d.mkdir(parents=True)
    (d / "config.json").write_bytes(data)
    return d


def test_model_dir_verifies_hashes(tmp_path: Path) -> None:
    d = install(tmp_path)
    assert local.model_dir("toy", tmp_path, raw=raw_cfg()) == d
    (d / "config.json").write_bytes(b'{"a": 2}')  # same size, different content
    with pytest.raises(UnsafeWeights):
        local.model_dir("toy", tmp_path, raw=raw_cfg())


def test_unknown_or_proposed_model_refused(tmp_path: Path) -> None:
    install(tmp_path)
    with pytest.raises(ModelNotApproved):
        local.model_dir("nope", tmp_path, raw=raw_cfg())
    with pytest.raises(ModelNotApproved):
        local.model_dir("toy", tmp_path, raw=raw_cfg("proposed"))


def test_load_passes_safe_kwargs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = install(tmp_path)
    monkeypatch.setattr(local, "model_dir", lambda name: d)
    seen: dict[str, Any] = {}

    class Fake:
        @classmethod
        def from_pretrained(cls, path: str, **kw: Any) -> str:
            seen.update(path=path, **kw)
            return "loaded"

    assert local.load(Fake, "toy", do_word_tokenize=False) == "loaded"
    assert seen == {
        "path": str(d),
        "local_files_only": True,
        "use_safetensors": True,
        "trust_remote_code": False,
        "dtype": torch.float32,
        "do_word_tokenize": False,
    }
    seen.clear()
    local.load(Fake, "toy", weights=False)
    assert seen == {"path": str(d), "local_files_only": True, "trust_remote_code": False}


RESERVED = [
    "local_files_only",
    "trust_remote_code",
    "use_safetensors",
    "dtype",
    "torch_dtype",
    "subfolder",
    "state_dict",
    "gguf_file",
    "variant",
    "revision",
    "cache_dir",
    "force_download",
    "token",
    "pretrained_model_name_or_path",
]


@pytest.mark.parametrize("key", RESERVED)
@pytest.mark.parametrize("weights", [True, False])
def test_reserved_extra_kwargs_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, weights: bool
) -> None:
    d = install(tmp_path)
    monkeypatch.setattr(local, "model_dir", lambda name: d)

    class Fake:
        @classmethod
        def from_pretrained(cls, path: str, **kw: Any) -> str:
            raise AssertionError("must not be reached")

    with pytest.raises(ValueError):
        local.load(Fake, "toy", weights=weights, **{key: True})


def test_unpinned_file_refused(tmp_path: Path) -> None:
    d = install(tmp_path)
    (d / "tokenizer_config.json").write_text("{}")
    with pytest.raises(UnsafeWeights):
        local.model_dir("toy", tmp_path, raw=raw_cfg())
    (d / "tokenizer_config.json").unlink()
    (d / "sub").mkdir()
    (d / "sub" / "x.json").write_text("{}")
    with pytest.raises(UnsafeWeights):
        local.model_dir("toy", tmp_path, raw=raw_cfg())


def test_symlink_refused(tmp_path: Path) -> None:
    d = install(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (d / "link").symlink_to(other)
    with pytest.raises(UnsafeWeights):
        local.model_dir("toy", tmp_path, raw=raw_cfg())
    (d / "link").unlink()
    (other / "config.json").write_bytes(b'{"a": 1}')
    (d / "config.json").unlink()
    (d / "config.json").symlink_to(other / "config.json")  # pinned name, but a symlink
    with pytest.raises(UnsafeWeights):
        local.model_dir("toy", tmp_path, raw=raw_cfg())
