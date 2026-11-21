"""Replay provider for tests and CI: fixtures instead of a model. It never loads anything.

A fixture `<case>.json` holds the raw model text of each attempt for one (prompt, image) pair.
Fixtures come only from synthetic panels drawn in code, never from the manga.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pipeline.providers.base import M, ModelSpec, fence_untrusted
from pipeline.providers.structured import structured_from_text

__all__ = ["ReplayMiss", "ReplayProvider", "record_case"]

_CASE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ReplayMiss(LookupError):
    """No recorded fixture matches this prompt and image."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_case(
    fixtures_dir: Path,
    case: str,
    prompt_sha: str,
    image_sha: str,
    content_sha: str,
    outputs: list[str],
) -> Path:
    """Write `<case>.json`. `case` is a plain token (it becomes a file name)."""
    if not _CASE.fullmatch(case):
        raise ValueError("case must be 1-64 letters, digits, '-' or '_'")
    if not outputs:
        raise ValueError("a fixture needs at least one output")
    fixture_key = _sha(
        json.dumps([prompt_sha, image_sha, content_sha], separators=(",", ":")).encode()
    )
    path = fixtures_dir / f"{case}.json"
    path.write_text(
        json.dumps(
            {
                "case": case,
                "prompt_sha256": prompt_sha,
                "image_sha256": image_sha,
                "content_sha256": content_sha,
                "fixture_key": fixture_key,
                "outputs": outputs,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class ReplayProvider:
    """Keyed by sha256 of: the instructions, the single image's bytes (b"" for text-only), and
    the fenced content, so recorded output can depend on the OCR text."""

    def __init__(self, fixtures_dir: Path) -> None:
        self._cases: dict[tuple[str, str, str], list[str]] = {}
        for f in sorted(fixtures_dir.glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            key = (d["prompt_sha256"], d["image_sha256"], d["content_sha256"])
            if key in self._cases:
                raise ValueError(f"{f.name}: duplicate fixture for the same prompt, image and text")
            self._cases[key] = list(d["outputs"])

    def _run(
        self,
        spec: ModelSpec,
        instructions: str,
        content: str,
        images: list[bytes],
        schema: type[M],
    ) -> M:
        key = (
            _sha(instructions.encode("utf-8")),
            _sha(images[0] if images else b""),
            _sha(fence_untrusted(content).encode("utf-8")),
        )
        outputs = self._cases.get(key)
        if outputs is None:
            raise ReplayMiss("no recorded fixture for this prompt, image and text")
        return structured_from_text(
            schema=schema, spec=spec, generate=lambda a: outputs[a - 1], max_attempts=len(outputs)
        )

    def llm_structured(
        self, spec: ModelSpec, instructions: str, content: str, schema: type[M]
    ) -> M:
        return self._run(spec, instructions, content, [], schema)

    def vlm_structured(
        self, spec: ModelSpec, instructions: str, content: str, images: list[bytes], schema: type[M]
    ) -> M:
        return self._run(spec, instructions, content, images, schema)
