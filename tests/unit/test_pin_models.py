from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_spec = importlib.util.spec_from_file_location(
    "pin_models", Path(__file__).parents[2] / "deploy/scripts/pin_models.py"
)
assert _spec and _spec.loader
pin_models = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pin_models)

A, B = "a" * 64, "b" * 64


def conv(src_sha: str, outputs: dict[str, Any], rev: str = "r1") -> dict[str, Any]:
    return {
        "revision": rev,
        "sources": [{"path": "x.bin", "sha256": src_sha}],
        "steps": [{"src": "x.bin", "dst": d} for d in outputs],
        "outputs": outputs,
    }


def test_prior_output_hash_kept_when_sources_unchanged() -> None:
    new = conv(A, {"m.safetensors": None})
    prior = conv(A, {"m.safetensors": B})
    assert pin_models.carry_outputs(new, prior)["m.safetensors"] == B


def test_prior_output_hash_dropped_when_source_changes() -> None:
    new = conv("c" * 64, {"m.safetensors": None})
    prior = conv(A, {"m.safetensors": B})
    assert pin_models.carry_outputs(new, prior)["m.safetensors"] is None


def test_prior_output_hash_dropped_when_revision_changes() -> None:
    new = conv(A, {"m.safetensors": None}, rev="r2")
    prior = conv(A, {"m.safetensors": B})
    assert pin_models.carry_outputs(new, prior)["m.safetensors"] is None


def test_outputs_cover_every_step_even_if_sources_yaml_lists_some() -> None:
    new = {
        **conv(A, {"a.safetensors": None, "b.safetensors": None}),
        "outputs": {"a.safetensors": B},
    }
    out = pin_models.carry_outputs(new, None)
    assert out == {"a.safetensors": B, "b.safetensors": None}


def test_jinja_chat_template_is_text_format(tmp_path: Path) -> None:
    from pipeline.providers.weights import FileSpec, UnsafeWeights, verify_file

    assert pin_models.FORMATS[".jinja"] == "text"
    body = b"{% for m in messages %}{{ m.content }}{% endfor %}"
    f = tmp_path / "chat_template.jinja"
    f.write_bytes(body)
    import hashlib

    spec = FileSpec(
        path="chat_template.jinja",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        format="text",
    )
    assert verify_file(f, spec) == f
    # non-UTF-8 content is refused even with a matching hash
    bad = tmp_path / "bad.jinja"
    bad.write_bytes(b"\xff\xfe\x00")
    bad_spec = FileSpec(
        path="bad.jinja",
        sha256=hashlib.sha256(b"\xff\xfe\x00").hexdigest(),
        size_bytes=3,
        format="text",
    )
    try:
        verify_file(bad, bad_spec)
    except UnsafeWeights:
        pass
    else:
        raise AssertionError("invalid utf-8 accepted")
