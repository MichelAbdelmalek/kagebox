"""Page sources: PDF (rendered in a sandboxed child process), CBZ and image folders.

``iter_pages`` yields (1-based source page number, RGB image with long side <= MAX_LONG_SIDE)
lazily, for an inclusive page range. Every failure is an ``UploadRejected`` whose message is safe
to show to the user; details (exit codes, child stderr) go to the log only.

Untrusted PDFs are never opened in this process: page counting, encryption probing and every
page render run in ``pipeline.ingest.pdf_child`` under rlimits, one child per call, so the CPU
limit applies per page and a crash only loses that page.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import resource
import shutil
import stat

# subprocess runs only our own child module, as an argument list without a shell.
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import filetype
from PIL import Image

from pipeline.common.uploads import (
    IMAGE_MIMES,
    ArchiveLimits,
    UploadRejected,
    reencode_image,
    safe_read_zip,
    sniff,
)

log = logging.getLogger(__name__)

SourceKind = Literal["pdf", "cbz", "images"]

# Limits are module constants so tests (and config) can tighten them. Anything the child must
# honour is passed to it as an argument: patching this module does not reach the child process.
MAX_PAGES = 1000
MAX_LONG_SIDE = 3000
RENDER_DPI = 300.0
MAX_PDF_BYTES = 1024**3
MAX_CBZ_BYTES = 2 * 1024**3
MAX_IMAGE_BYTES = 200 * 1024**2
MAX_RENDERED_PNG_BYTES = 128 * 1024**2

CHILD_TIMEOUT_S = 120.0  # wall clock: RLIMIT_CPU does not stop a sleeping or blocked child
CHILD_CPU_S = 60
CHILD_AS_BYTES = 2 * 1024**3
CHILD_FSIZE_BYTES = 256 * 1024**2
CHILD_NOFILE = 64
CHILD_MODULE = "pipeline.ingest.pdf_child"
CHILD_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}

_CHILD_ERRORS = {
    "encrypted": "encrypted PDFs are not supported",
    "invalid": "the PDF could not be read",
    "range": "the requested page range is outside the document",
    "page": "a PDF page could not be rendered",
}


# ---------------------------------------------------------------------------------------------
# public API


def detect_kind(path: Path) -> SourceKind:
    """Classify by content, never by name: a directory is an image folder, else magic bytes."""
    if path.is_dir() and not path.is_symlink():
        return "images"
    head = _read_capped(path, 8, truncate=True)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "cbz"
    raise UploadRejected("unsupported source: expected a PDF, a CBZ archive or an image folder")


def iter_pages(
    path: Path, kind: SourceKind, page_start: int, page_end: int
) -> Iterator[tuple[int, Image.Image]]:
    if kind == "pdf":
        return _iter_pdf(path, page_start, page_end)
    if kind == "cbz":
        return _iter_cbz(path, page_start, page_end)
    if kind == "images":
        return _iter_images(path, page_start, page_end)
    raise UploadRejected("unsupported source kind")


# ---------------------------------------------------------------------------------------------
# shared helpers


def _check_range(total: int, page_start: int, page_end: int) -> None:
    if total > MAX_PAGES:
        raise UploadRejected(f"document has too many pages (limit {MAX_PAGES})")
    if total == 0:
        raise UploadRejected("document has no pages")
    if not 1 <= page_start <= page_end <= total:
        raise UploadRejected(
            f"page range {page_start}-{page_end} is outside the document (pages 1-{total})"
        )


def _fit(image: Image.Image) -> Image.Image:
    """RGB, long side capped at MAX_LONG_SIDE. Applied to every source, child output included."""
    rgb = image.convert("RGB")
    long_side = max(rgb.size)
    if long_side <= MAX_LONG_SIDE:
        return rgb
    s = MAX_LONG_SIDE / long_side
    size = (max(1, round(rgb.width * s)), max(1, round(rgb.height * s)))
    return rgb.resize(size, Image.Resampling.LANCZOS)


def _decode(data: bytes) -> Image.Image:
    """Decode bytes already re-encoded or produced by our child (MAX_IMAGE_PIXELS still on)."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.load()
            fitted = _fit(im)
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise UploadRejected("image could not be decoded safely") from exc
    if fmt != "PNG":
        raise UploadRejected("image could not be decoded safely")
    return fitted


def _read_capped(path: Path, max_bytes: int, *, truncate: bool = False) -> bytes:
    """Read a regular file without following a final symlink; refuse (or truncate) past max."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise UploadRejected("source file could not be opened") from exc
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise UploadRejected("source is not a regular file")
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        if truncate:
            return data[:max_bytes]
        raise UploadRejected(f"file is larger than {max_bytes} bytes")
    return data


def _natural_key(name: str) -> tuple[tuple[int, int, str], ...]:
    parts = re.split(r"(\d+)", name.casefold())
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts if p)


# ---------------------------------------------------------------------------------------------
# CBZ and image folders


def _iter_cbz(path: Path, page_start: int, page_end: int) -> Iterator[tuple[int, Image.Image]]:
    entries = safe_read_zip(_read_capped(path, MAX_CBZ_BYTES), ArchiveLimits())
    images = sorted(
        ((name, data) for name, data in entries if _is_image(data)),
        key=lambda e: _natural_key(e[0]),
    )
    _check_range(len(images), page_start, page_end)
    for index in range(page_start, page_end + 1):
        _, data = images[index - 1]
        sniff(data, IMAGE_MIMES, MAX_IMAGE_BYTES)
        yield index, _decode(reencode_image(data))


def _is_image(data: bytes) -> bool:
    kind = filetype.guess(data[:8192]) if data else None
    return kind is not None and kind.mime in IMAGE_MIMES


def _iter_images(folder: Path, page_start: int, page_end: int) -> Iterator[tuple[int, Image.Image]]:
    if folder.is_symlink() or not folder.is_dir():
        raise UploadRejected("image source must be a folder")
    files = []
    for entry in os.scandir(folder):
        # regular files only: no symlinks, no subdirectories, no devices
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        if _is_image(_read_capped(Path(entry.path), 8192, truncate=True)):
            files.append(entry.name)
    files.sort(key=_natural_key)
    _check_range(len(files), page_start, page_end)
    for index in range(page_start, page_end + 1):
        data = _read_capped(folder / files[index - 1], MAX_IMAGE_BYTES)
        sniff(data, IMAGE_MIMES, MAX_IMAGE_BYTES)
        yield index, _decode(reencode_image(data))


# ---------------------------------------------------------------------------------------------
# PDF via the sandboxed child


def _child_argv(args: list[str]) -> list[str]:
    # -I: isolated mode (ignores PYTHON* env vars, user site and the current directory).
    return [sys.executable, "-I", "-m", CHILD_MODULE, *args]


def _limits_for_child() -> Callable[[], None]:
    """Snapshot the (patchable) limits now: the preexec hook runs after fork."""
    limits = (
        (resource.RLIMIT_AS, CHILD_AS_BYTES),
        (resource.RLIMIT_CPU, CHILD_CPU_S),
        (resource.RLIMIT_FSIZE, CHILD_FSIZE_BYTES),
        (resource.RLIMIT_NOFILE, CHILD_NOFILE),
        (resource.RLIMIT_CORE, 0),
    )

    def apply() -> None:  # pragma: no cover - runs in the forked child before exec
        for which, value in limits:
            resource.setrlimit(which, (value, value))

    return apply


def _run_child(args: list[str], cwd: Path) -> dict[str, object]:
    argv = _child_argv(args)
    try:
        # Fixed argv list (our own module, file paths as separate arguments), never a shell.
        proc = subprocess.run(  # noqa: S603  # nosec B603
            argv,
            capture_output=True,
            timeout=CHILD_TIMEOUT_S,
            close_fds=True,
            env=dict(CHILD_ENV),
            cwd=cwd,
            preexec_fn=_limits_for_child(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("pdf child timed out after %ss (args=%s)", CHILD_TIMEOUT_S, args[:1])
        raise UploadRejected("PDF processing timed out") from exc
    except OSError as exc:
        log.error("pdf child could not start: %s", exc)
        raise UploadRejected("the PDF could not be processed") from exc

    try:
        result = json.loads(proc.stdout.decode("utf-8", "replace").strip() or "null")
    except json.JSONDecodeError:
        result = None
    if isinstance(result, dict) and result.get("ok") is False and proc.returncode == 2:
        code = result.get("error")
        log.info("pdf child refused: %s", code)
        raise UploadRejected(_CHILD_ERRORS.get(str(code), "the PDF could not be processed"))
    if proc.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True:
        log.warning(
            "pdf child failed: rc=%s stderr=%r",
            proc.returncode,
            proc.stderr[-2000:].decode("utf-8", "replace"),
        )
        raise UploadRejected("the PDF could not be processed")
    return result


def _iter_pdf(path: Path, page_start: int, page_end: int) -> Iterator[tuple[int, Image.Image]]:
    try:
        st = path.lstat()
    except OSError as exc:
        raise UploadRejected("source file could not be opened") from exc
    if not stat.S_ISREG(st.st_mode):
        raise UploadRejected("source is not a regular file")
    if st.st_size > MAX_PDF_BYTES:
        raise UploadRejected(f"file is larger than {MAX_PDF_BYTES} bytes")
    pdf = str(path.resolve())
    workdir = Path(tempfile.mkdtemp(prefix="kagebox-pdf-"))
    try:
        probe = _run_child(["probe", pdf], workdir)
        total = probe.get("pages")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise UploadRejected("the PDF could not be processed")
        _check_range(total, page_start, page_end)
        for index in range(page_start, page_end + 1):
            out = workdir / f"page-{index:05d}.png"
            _run_child(
                ["render", pdf, str(index), str(out), repr(RENDER_DPI), str(MAX_LONG_SIDE)],
                workdir,
            )
            data = _read_capped(out, MAX_RENDERED_PNG_BYTES)
            out.unlink()
            yield index, _decode(data)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
