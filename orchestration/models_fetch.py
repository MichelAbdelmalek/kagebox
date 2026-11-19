"""One-shot job: download pinned model files listed in configs/models.yaml into /models.

Each file is fetched from Hugging Face at a pinned commit, streamed to a temporary file while
hashing, checked against its pinned sha256 and size (and sniffed for pickle content), and only
then moved into place. Re-runs skip files that already verify. Redirects are followed only to
Hugging Face hosts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from pipeline.providers.registry import load_yaml
from pipeline.providers.weights import (
    FileSpec,
    LocalModel,
    UnsafeWeights,
    looks_like_pickle,
    verify_file,
)

log = logging.getLogger("models_fetch")

HF_BASE = "https://huggingface.co"
ALLOWED_HOST_SUFFIXES = ("huggingface.co", ".hf.co")
MAX_REDIRECTS = 5


def load_models(raw: dict[str, object]) -> list[LocalModel]:
    models = raw.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError("models.yaml: `models` must be a mapping")
    return [LocalModel.model_validate({"name": k, **v}) for k, v in models.items()]


def _host_allowed(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        host == "huggingface.co" or any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)
    )


def _dest(root: Path, m: LocalModel, f: FileSpec) -> Path:
    base = (root / m.name / m.revision).resolve()
    target = (base / f.path).resolve()
    if not target.is_relative_to(base):  # FileSpec already refuses "..", this is belt and braces
        raise UnsafeWeights(f"{f.path}: escapes the model directory")
    return target


def fetch_file(client: httpx.Client, m: LocalModel, f: FileSpec, root: Path) -> Path:
    target = _dest(root, m, f)
    if target.exists():
        try:
            return verify_file(target, f)
        except UnsafeWeights:
            log.warning("%s/%s failed verification; downloading again", m.name, f.path)
            target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    url = f"{HF_BASE}/{m.repo}/resolve/{m.revision}/{quote(f.path)}"
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not _host_allowed(url):
                raise UnsafeWeights(f"{f.path}: refusing to download from {urlsplit(url).hostname}")
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = str(r.url.join(r.headers["location"]))
                    continue
                r.raise_for_status()
                h, n, head = hashlib.sha256(), 0, b""
                with part.open("wb") as out:
                    for chunk in r.iter_bytes(1 << 20):
                        n += len(chunk)
                        if n > f.size_bytes:
                            raise UnsafeWeights(f"{f.path}: larger than pinned size")
                        if len(head) < 8:
                            head = (head + chunk)[:8]
                        h.update(chunk)
                        out.write(chunk)
                break
        else:
            raise UnsafeWeights(f"{f.path}: too many redirects")
        if n != f.size_bytes:
            raise UnsafeWeights(f"{f.path}: size mismatch")
        if h.hexdigest() != f.sha256:
            raise UnsafeWeights(f"{f.path}: checksum mismatch")
        if looks_like_pickle(head, part):
            raise UnsafeWeights(f"{f.path}: file content is a pickle")
        os.replace(part, target)
        target.chmod(0o444)
        return target
    finally:
        part.unlink(missing_ok=True)


def fetch_all(models: Iterable[LocalModel], root: Path, client: httpx.Client) -> list[Path]:
    out: list[Path] = []
    for m in models:
        if m.status != "approved":
            log.info("skipping %s (status %s)", m.name, m.status)
            continue
        for f in m.files:
            out.append(fetch_file(client, m, f, root))
            log.info("ok %s/%s", m.name, f.path)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs carry CDN signatures
    models = load_models(load_yaml("models.yaml"))
    only = {n.strip() for n in os.environ.get("KAGEBOX_MODELS_ONLY", "").split(",") if n.strip()}
    if only:
        unknown = only - {m.name for m in models}
        if unknown:
            log.error("unknown model(s): %s", ", ".join(sorted(unknown)))
            return 1
        models = [m for m in models if m.name in only]
    root = Path(os.environ.get("KAGEBOX_MODELS_DIR", "/models"))
    headers = {"user-agent": "kagebox-models-fetch"}
    with httpx.Client(
        timeout=httpx.Timeout(60.0, connect=10.0), headers=headers, follow_redirects=False
    ) as client:
        try:
            paths = fetch_all(models, root, client)
        except (UnsafeWeights, httpx.HTTPError) as exc:
            log.error("model fetch failed: %s", exc)
            return 1
    log.info("%d file(s) verified in %s", len(paths), root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
