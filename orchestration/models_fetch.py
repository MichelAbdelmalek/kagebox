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
    Conversion,
    FileSpec,
    LocalModel,
    UnsafeWeights,
    load_conversions,
    validate_content,
    verify_file,
)

log = logging.getLogger("models_fetch")

HF_BASE = "https://huggingface.co"
ALLOWED_HOST_SUFFIXES = (".huggingface.co", ".hf.co")  # dot boundary: no lookalike domains
MAX_REDIRECTS = 5
REFERENCE_DIR = "_reference"  # keeps reference copies apart from same-named sources


def load_models(raw: dict[str, object]) -> list[LocalModel]:
    models = raw.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError("models.yaml: `models` must be a mapping")
    return [LocalModel.model_validate({"name": k, **v}) for k, v in models.items()]


def _host_allowed(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        host in ("huggingface.co", "hf.co") or any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)
    )


def _target(root: Path, name: str, revision: str, rel: str) -> Path:
    base = (root / name / revision).resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):  # specs already refuse "..", this is belt and braces
        raise UnsafeWeights(f"{rel}: escapes the model directory")
    return target


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(
    client: httpx.Client,
    repo: str,
    revision: str,
    rel: str,
    sha256: str,
    size: int,
    target: Path,
    *,
    fmt: str,
) -> Path:
    """Fetch one pinned file. Pickle content is refused unless it is a quarantined source."""
    if target.exists():
        if target.stat().st_size == size and _sha256(target) == sha256:
            return target
        log.warning("%s failed verification; downloading again", rel)
        target.chmod(0o644)
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    url = f"{HF_BASE}/{repo}/resolve/{revision}/{quote(rel)}"
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not _host_allowed(url):
                raise UnsafeWeights(f"{rel}: refusing to download from {urlsplit(url).hostname}")
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = str(r.url.join(r.headers["location"]))
                    continue
                r.raise_for_status()
                h, n, head = hashlib.sha256(), 0, b""
                with part.open("wb") as out:
                    for chunk in r.iter_bytes(1 << 20):
                        n += len(chunk)
                        if n > size:
                            raise UnsafeWeights(f"{rel}: larger than pinned size")
                        if len(head) < 8:
                            head = (head + chunk)[:8]
                        h.update(chunk)
                        out.write(chunk)
                break
        else:
            raise UnsafeWeights(f"{rel}: too many redirects")
        if n != size:
            raise UnsafeWeights(f"{rel}: size mismatch")
        if h.hexdigest() != sha256:
            raise UnsafeWeights(f"{rel}: checksum mismatch")
        if fmt != "torch-pickle":  # pickle is expected only for quarantined conversion sources
            validate_content(part, fmt)
        os.replace(part, target)
        target.chmod(0o444)
        return target
    finally:
        part.unlink(missing_ok=True)


def fetch_file(client: httpx.Client, m: LocalModel, f: FileSpec, root: Path) -> Path:
    target = _target(root, m.name, m.revision, f.path)
    path = download(
        client, m.repo, m.revision, f.path, f.sha256, f.size_bytes, target, fmt=f.format
    )
    return verify_file(path, f)


def fetch_conversion(c: Conversion, quarantine: Path, models: Path, client: httpx.Client) -> None:
    """Pickle sources and reference copies go to quarantine; data files go to the models volume."""
    for s in c.sources:
        download(
            client,
            c.repo,
            c.revision,
            s.path,
            s.sha256,
            s.size_bytes,
            _target(quarantine, c.name, c.revision, s.path),
            fmt=s.format,
        )
    if c.reference:
        r = c.reference
        download(
            client,
            r.repo,
            r.revision,
            r.path,
            r.sha256,
            r.size_bytes,
            _target(quarantine, c.name, c.revision, f"{REFERENCE_DIR}/{r.path}"),
            fmt="safetensors",
        )
    for f in c.data_files:
        path = download(
            client,
            c.repo,
            c.revision,
            f.path,
            f.sha256,
            f.size_bytes,
            _target(models, c.name, c.revision, f.path),
            fmt=f.format,
        )
        verify_file(path, f)


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
    raw = load_yaml("models.yaml")
    models, conversions = load_models(raw), load_conversions(raw)
    only = {n.strip() for n in os.environ.get("KAGEBOX_MODELS_ONLY", "").split(",") if n.strip()}
    unknown = only - {m.name for m in models} - {c.name for c in conversions}
    if unknown:
        log.error("unknown model(s): %s", ", ".join(sorted(unknown)))
        return 1
    if only:
        models = [m for m in models if m.name in only]
        conversions = [c for c in conversions if c.name in only]
    root = Path(os.environ.get("KAGEBOX_MODELS_DIR", "/models"))
    quarantine = Path(os.environ.get("KAGEBOX_QUARANTINE_DIR", "/quarantine"))
    headers = {"user-agent": "kagebox-models-fetch"}
    with httpx.Client(
        timeout=httpx.Timeout(60.0, connect=10.0), headers=headers, follow_redirects=False
    ) as client:
        try:
            paths = fetch_all(models, root, client)
            for c in conversions:
                if c.status == "approved":
                    fetch_conversion(c, quarantine, root, client)
                    log.info("ok %s (sources quarantined for conversion)", c.name)
        except (UnsafeWeights, httpx.HTTPError) as exc:
            log.error("model fetch failed: %s", exc)
            return 1
    log.info("%d model file(s) verified in %s", len(paths), root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
