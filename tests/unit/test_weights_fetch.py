from __future__ import annotations

import hashlib
import io
import pickle
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from orchestration.models_fetch import fetch_all, load_models
from pipeline.providers.weights import LocalModel, UnsafeWeights, looks_like_pickle, verify_file

REV = "a" * 40
SAFE = b"\x08\x00\x00\x00\x00\x00\x00\x00{}      "  # minimal valid safetensors (empty)
SAFE_SHA = hashlib.sha256(SAFE).hexdigest()


def model(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "approved",
        "repo": "org/model",
        "revision": REV,
        "license": "Apache-2.0",
        "source": "https://huggingface.co/org/model",
        "files": [
            {
                "path": "model.safetensors",
                "sha256": SAFE_SHA,
                "size_bytes": len(SAFE),
                "format": "safetensors",
            }
        ],
    }
    base.update(over)
    return base


def client(routes: dict[str, httpx.Response], seen: list[str] | None = None) -> httpx.Client:
    def handle(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        return routes.get(str(req.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)


URL = f"https://huggingface.co/org/model/resolve/{REV}/model.safetensors"


# --- manifest validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["../x.safetensors", "/abs.safetensors", "a/../../b.safetensors", ".hidden.safetensors"]
)
def test_unsafe_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError, match="unsafe"):
        load_models(
            {
                "models": {
                    "m": model(
                        files=[
                            {
                                "path": path,
                                "sha256": SAFE_SHA,
                                "size_bytes": 1,
                                "format": "safetensors",
                            }
                        ]
                    )
                }
            }
        )


@pytest.mark.parametrize("path", ["model.pt", "model.pth", "model.ckpt", "x.pkl"])
def test_pickle_suffixes_never_allowed(path: str) -> None:
    with pytest.raises(ValidationError, match="pickle"):
        load_models(
            {
                "models": {
                    "m": model(
                        files=[
                            {
                                "path": path,
                                "sha256": SAFE_SHA,
                                "size_bytes": 1,
                                "format": "safetensors",
                            }
                        ]
                    )
                }
            }
        )


def test_pytorch_bin_cannot_pose_as_safetensors() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        load_models(
            {
                "models": {
                    "m": model(
                        files=[
                            {
                                "path": "pytorch_model.bin",
                                "sha256": SAFE_SHA,
                                "size_bytes": 1,
                                "format": "safetensors",
                            }
                        ]
                    )
                }
            }
        )


def test_gated_format_needs_reviewed_exception() -> None:
    onnx = [{"path": "model.onnx", "sha256": SAFE_SHA, "size_bytes": 1, "format": "onnx"}]
    with pytest.raises(ValidationError, match="format_exception"):
        load_models({"models": {"m": model(files=onnx)}})
    exc = {
        "formats": ["onnx"],
        "justification": "No safetensors release; ONNX is not pickle.",
        "reviewed_by": "michel",
        "reviewed_at": "2026-11-19",
    }
    [m] = load_models({"models": {"m": model(files=onnx, format_exception=exc)}})
    assert m.format_exception is not None


def test_revision_must_be_full_commit_sha() -> None:
    with pytest.raises(ValidationError):
        load_models({"models": {"m": model(revision="main")}})


# --- pickle sniffing ---------------------------------------------------------------------------


def test_pickle_sniffing(tmp_path: Path) -> None:
    raw = pickle.dumps({"a": 1}, protocol=4)
    assert looks_like_pickle(raw[:8])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("archive/data.pkl", raw)
    p = tmp_path / "torch.zip"
    p.write_bytes(buf.getvalue())
    assert looks_like_pickle(p.read_bytes()[:8], p)
    assert not looks_like_pickle(SAFE[:8])


def test_verify_refuses_renamed_pickle(tmp_path: Path) -> None:
    raw = pickle.dumps([1, 2, 3], protocol=4)
    p = tmp_path / "model.safetensors"
    p.write_bytes(raw)
    [m] = load_models(
        {
            "models": {
                "m": model(
                    files=[
                        {
                            "path": "model.safetensors",
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "size_bytes": len(raw),
                            "format": "safetensors",
                        }
                    ]
                )
            }
        }
    )
    with pytest.raises(UnsafeWeights, match=r"pickle|safetensors header"):
        verify_file(p, m.files[0])


# --- fetching ----------------------------------------------------------------------------------


def test_fetch_verifies_and_is_idempotent(tmp_path: Path) -> None:
    [m] = load_models({"models": {"m": model()}})
    seen: list[str] = []
    c = client({URL: httpx.Response(200, content=SAFE)}, seen)
    [path] = fetch_all([m], tmp_path, c)
    assert path == tmp_path / "m" / REV / "model.safetensors"
    assert path.read_bytes() == SAFE
    fetch_all([m], tmp_path, c)
    assert len(seen) == 1  # second run verified the existing file, no download


def test_fetch_follows_redirect_to_hf_cdn_only(tmp_path: Path) -> None:
    [m] = load_models({"models": {"m": model()}})
    cdn = "https://cas-bridge.xethub.hf.co/x/model"
    ok = client(
        {
            URL: httpx.Response(302, headers={"location": cdn}),
            cdn: httpx.Response(200, content=SAFE),
        }
    )
    assert fetch_all([m], tmp_path / "a", ok)
    evil = client({URL: httpx.Response(302, headers={"location": "https://evil.example/m"})})
    with pytest.raises(UnsafeWeights, match="refusing"):
        fetch_all([m], tmp_path / "b", evil)


@pytest.mark.parametrize("body", [SAFE[:-1] + b"X", SAFE + b"extra"])
def test_tampered_download_refused_and_not_kept(tmp_path: Path, body: bytes) -> None:
    [m] = load_models({"models": {"m": model()}})
    with pytest.raises(UnsafeWeights):
        fetch_all([m], tmp_path, client({URL: httpx.Response(200, content=body)}))
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == []


def test_proposed_models_are_not_fetched(tmp_path: Path) -> None:
    [m] = load_models({"models": {"m": model(status="proposed")}})
    seen: list[str] = []
    assert fetch_all([m], tmp_path, client({}, seen)) == [] and seen == []


def test_repo_config_validates() -> None:
    from pipeline.providers.registry import load_yaml

    load_models(load_yaml("models.yaml"))  # the committed manifest must always parse


def test_frozen_models() -> None:
    [m] = load_models({"models": {"m": model()}})
    assert isinstance(m, LocalModel)
    with pytest.raises(ValidationError):
        m.files[0].sha256 = "0" * 64  # type: ignore[misc]


# --- conversion sources go to quarantine only ----------------------------------------------------


def test_conversion_sources_land_in_quarantine_only(tmp_path: Path) -> None:
    import pickle as _pickle

    from orchestration.models_convert import load_conversions
    from orchestration.models_fetch import fetch_conversion

    blob = _pickle.dumps({"w": 1}, protocol=4)  # a pickle: allowed only as a quarantined source
    cfg = b'{"a": 1}'
    raw = {
        "conversions": {
            "toy": {
                "status": "approved",
                "repo": "org/toy",
                "revision": REV,
                "license": "MIT",
                "source": "https://huggingface.co/org/toy",
                "sources": [
                    {
                        "path": "pytorch_model.bin",
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "size_bytes": len(blob),
                        "format": "torch-pickle",
                    }
                ],
                "data_files": [
                    {
                        "path": "config.json",
                        "sha256": hashlib.sha256(cfg).hexdigest(),
                        "size_bytes": len(cfg),
                        "format": "json",
                    }
                ],
                "steps": [{"src": "pytorch_model.bin", "dst": "model.safetensors"}],
                "outputs": {"model.safetensors": None},
            }
        }
    }
    [conv] = load_conversions(raw)
    base = f"https://huggingface.co/org/toy/resolve/{REV}/"
    c = client(
        {
            base + "pytorch_model.bin": httpx.Response(200, content=blob),
            base + "config.json": httpx.Response(200, content=cfg),
        }
    )
    q, m = tmp_path / "quarantine", tmp_path / "models"
    fetch_conversion(conv, q, m, c)
    assert (q / "toy" / REV / "pytorch_model.bin").read_bytes() == blob
    assert (m / "toy" / REV / "config.json").read_bytes() == cfg
    assert not any(p.suffix == ".bin" for p in m.rglob("*"))
    assert not (q / "toy" / REV / "config.json").exists()


def test_conversion_source_hash_still_enforced(tmp_path: Path) -> None:
    import pickle as _pickle

    from orchestration.models_convert import load_conversions
    from orchestration.models_fetch import fetch_conversion

    blob = _pickle.dumps({"w": 1}, protocol=4)
    raw = {
        "conversions": {
            "toy": {
                "status": "approved",
                "repo": "org/toy",
                "revision": REV,
                "license": "MIT",
                "source": "https://huggingface.co/org/toy",
                "sources": [
                    {
                        "path": "pytorch_model.bin",
                        "sha256": "0" * 64,
                        "size_bytes": len(blob),
                        "format": "torch-pickle",
                    }
                ],
                "steps": [{"src": "pytorch_model.bin", "dst": "model.safetensors"}],
                "outputs": {"model.safetensors": None},
            }
        }
    }
    [conv] = load_conversions(raw)
    url = f"https://huggingface.co/org/toy/resolve/{REV}/pytorch_model.bin"
    with pytest.raises(UnsafeWeights, match="checksum"):
        fetch_conversion(
            conv, tmp_path / "q", tmp_path / "m", client({url: httpx.Response(200, content=blob)})
        )


# --- review fixes ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evilhuggingface.co", "xhf.co", "huggingface.co.evil.example"])
def test_lookalike_hosts_refused(host: str) -> None:
    from orchestration.models_fetch import _host_allowed

    assert not _host_allowed(f"https://{host}/x")
    assert _host_allowed("https://huggingface.co/x")
    assert _host_allowed("https://cdn-lfs.huggingface.co/x")
    assert _host_allowed("https://us.aws.cdn.hf.co/x")


def test_valid_safetensors_with_0x80_header_byte_is_not_a_pickle(tmp_path: Path) -> None:
    import struct

    header = b'{"__metadata__":{"k":"' + b"v" * 100 + b'"}}'
    header += b" " * (0x280 - len(header))  # length 0x0280: first byte 0x80, second 0x02
    data = struct.pack("<Q", len(header)) + header
    assert data[:2] == b"\x80\x02"
    p = tmp_path / "model.safetensors"
    p.write_bytes(data)
    [m] = load_models(
        {
            "models": {
                "m": model(
                    files=[
                        {
                            "path": "model.safetensors",
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "size_bytes": len(data),
                            "format": "safetensors",
                        }
                    ]
                )
            }
        }
    )
    assert verify_file(p, m.files[0]) == p


def test_protocol0_pickle_posing_as_json_is_refused(tmp_path: Path) -> None:
    raw = pickle.dumps({"a": 1}, protocol=0)
    p = tmp_path / "config.json"
    p.write_bytes(raw)
    [m] = load_models(
        {
            "models": {
                "m": model(
                    files=[
                        {
                            "path": "config.json",
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "size_bytes": len(raw),
                            "format": "json",
                        }
                    ]
                )
            }
        }
    )
    with pytest.raises(UnsafeWeights):
        verify_file(p, m.files[0])


def test_reference_and_source_with_same_path_do_not_collide(tmp_path: Path) -> None:
    import pickle as _pickle

    from orchestration.models_convert import load_conversions
    from orchestration.models_fetch import fetch_conversion

    src = _pickle.dumps({"w": 1}, protocol=4)
    ref = b"\x08\x00\x00\x00\x00\x00\x00\x00{}      "
    raw = {
        "conversions": {
            "toy": {
                "status": "approved",
                "repo": "org/toy",
                "revision": REV,
                "license": "MIT",
                "source": "https://huggingface.co/org/toy",
                "sources": [
                    {
                        "path": "model.safetensors.bin",
                        "sha256": hashlib.sha256(src).hexdigest(),
                        "size_bytes": len(src),
                        "format": "torch-pickle",
                    }
                ],
                "steps": [{"src": "model.safetensors.bin", "dst": "model.safetensors"}],
                "outputs": {"model.safetensors": None},
                "reference": {
                    "step_dst": "model.safetensors",
                    "repo": "org/toy",
                    "revision": "c" * 40,
                    "path": "model.safetensors",
                    "sha256": hashlib.sha256(ref).hexdigest(),
                    "size_bytes": len(ref),
                },
            }
        }
    }
    [conv] = load_conversions(raw)
    c = client(
        {
            f"https://huggingface.co/org/toy/resolve/{REV}/model.safetensors.bin": httpx.Response(
                200, content=src
            ),
            f"https://huggingface.co/org/toy/resolve/{'c' * 40}/model.safetensors": httpx.Response(
                200, content=ref
            ),
        }
    )
    q = tmp_path / "q"
    fetch_conversion(conv, q, tmp_path / "m", c)
    assert (q / "toy" / REV / "_reference" / "model.safetensors").read_bytes() == ref
    assert not (q / "toy" / REV / "model.safetensors").exists()


def test_verify_installed_detects_tampering(tmp_path: Path) -> None:
    from orchestration.check_models import verify_installed

    [m] = load_models({"models": {"m": model()}})
    d = tmp_path / "m" / REV
    d.mkdir(parents=True)
    (d / "model.safetensors").write_bytes(SAFE)
    assert verify_installed(m, tmp_path) == d
    (d / "model.safetensors").write_bytes(SAFE[:-1] + b"!")
    with pytest.raises(UnsafeWeights):
        verify_installed(m, tmp_path)
