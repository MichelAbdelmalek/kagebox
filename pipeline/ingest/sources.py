"""Page sources: PDF (rendered in a sandboxed child process), CBZ and image folders.

``iter_pages`` yields (1-based source page number, RGB image with long side <= MAX_LONG_SIDE)
lazily, for an inclusive page range. Every failure is an ``UploadRejected`` whose message is safe
to show to the user; details (exit codes, child stderr) go to the log only.

Untrusted PDFs are never opened in this process: page counting, encryption probing and every
page render run in ``pipeline.ingest.pdf_child`` under rlimits, one child per call, so the CPU
limit applies per page and a crash only loses that page.
"""

from __future__ import annotations

import fcntl
import io
import json
import logging
import os
import re
import shutil
import signal
import stat

# subprocess runs only our own child module, as an argument list without a shell.
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import filetype
from PIL import Image

from pipeline.common.uploads import (
    IMAGE_MIMES,
    ArchiveLimits,
    UploadRejected,
    decode_image,
    safe_read_zip,
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
DOC_DEADLINE_S = 1800.0  # wall clock summed over the probe and every page child of one document
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


def _decode_child_png(data: bytes) -> Image.Image:
    """Decode a PNG our child rendered. It was told the size cap; refuse rather than downscale."""
    try:
        im = Image.open(io.BytesIO(data), formats=("PNG",))
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise UploadRejected("image could not be decoded safely") from exc
    with im:
        if max(im.size) > MAX_LONG_SIDE:
            raise UploadRejected("image could not be decoded safely")
        try:
            im.load()
        except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
            raise UploadRejected("image could not be decoded safely") from exc
        return im.convert("RGB")


def _read_capped(path: Path, max_bytes: int, *, truncate: bool = False) -> bytes:
    """Read a regular file without following a final symlink; refuse (or truncate) past max."""
    # O_NONBLOCK: opening a FIFO for reading would otherwise block until a writer appears.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise UploadRejected("source file could not be opened") from exc
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UploadRejected("source is not a regular file")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        if truncate:
            return data[:max_bytes]
        raise UploadRejected(f"file is larger than {max_bytes} bytes")
    return data


_ASCII_DIGITS = re.compile(r"([0-9]+)")


def _natural_key(name: str) -> tuple[tuple[int, int, str], ...]:
    """Digit runs compare by value without int(): (length without leading zeros, digits).

    Only ASCII digits count as numbers, so '²' and other Unicode digits sort as text, and a
    hostile 5000-digit name cannot hit int()'s digit limit.
    """
    key = []
    for part in _ASCII_DIGITS.split(name.casefold()):
        if not part:
            continue
        if _ASCII_DIGITS.fullmatch(part):
            digits = part.lstrip("0") or "0"
            key.append((0, len(digits), digits))
        else:
            key.append((1, 0, part))
    return tuple(key)


# ---------------------------------------------------------------------------------------------
# CBZ and image folders


def _iter_cbz(path: Path, page_start: int, page_end: int) -> Iterator[tuple[int, Image.Image]]:
    entries = safe_read_zip(_read_capped(path, MAX_CBZ_BYTES), ArchiveLimits())
    images = [(name, data) for name, data in entries if _is_page_image(data)]
    _log_skipped(len(entries) - len(images))
    images.sort(key=lambda e: _natural_key(e[0]))
    _check_range(len(images), page_start, page_end)
    for index in range(page_start, page_end + 1):
        _, data = images[index - 1]
        yield index, _fit(decode_image(data, IMAGE_MIMES, MAX_IMAGE_BYTES))


def _is_page_image(data: bytes) -> bool:
    """True for png/jpeg/webp, False for non-images (ComicInfo.xml, __MACOSX/...).

    Any other image type is a page we cannot read: refuse instead of silently shifting the page
    numbers of everything after it.
    """
    kind = filetype.guess(data[:8192]) if data else None
    if kind is None or not kind.mime.startswith("image/"):
        return False
    if kind.mime not in IMAGE_MIMES:
        raise UploadRejected("unsupported image format: pages must be PNG, JPEG or WebP")
    return True


def _log_skipped(count: int) -> None:
    if count:
        log.info("skipped %d non-image entries", count)


def _iter_images(folder: Path, page_start: int, page_end: int) -> Iterator[tuple[int, Image.Image]]:
    if folder.is_symlink() or not folder.is_dir():
        raise UploadRejected("image source must be a folder")
    files = []
    skipped = 0
    for entry in os.scandir(folder):
        # regular files only: no symlinks, no subdirectories, no devices
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            skipped += 1
            continue
        if _is_page_image(_read_capped(Path(entry.path), 8192, truncate=True)):
            files.append(entry.name)
        else:
            skipped += 1
    _log_skipped(skipped)
    files.sort(key=_natural_key)
    _check_range(len(files), page_start, page_end)
    for index in range(page_start, page_end + 1):
        data = _read_capped(folder / files[index - 1], MAX_IMAGE_BYTES)
        yield index, _fit(decode_image(data, IMAGE_MIMES, MAX_IMAGE_BYTES))


# ---------------------------------------------------------------------------------------------
# PDF via the sandboxed child


def _child_argv(args: list[str]) -> list[str]:
    # -I: isolated mode (ignores PYTHON* env vars, user site and the current directory).
    return [sys.executable, "-I", "-m", CHILD_MODULE, *args]


class _Budget:
    """Wall-clock budget shared by the probe and all page children of one document."""

    def __init__(self) -> None:
        self.remaining = DOC_DEADLINE_S

    def timeout(self) -> tuple[float, bool]:
        """(timeout for the next child, whether the document budget is what binds)."""
        if self.remaining <= 0:
            raise UploadRejected("document took too long to render")
        return min(CHILD_TIMEOUT_S, self.remaining), self.remaining < CHILD_TIMEOUT_S


def _run_child(args: list[str], cwd: Path, budget: _Budget | None = None) -> dict[str, object]:
    budget = budget or _Budget()
    timeout, doc_bound = budget.timeout()
    # The child applies these to itself before importing pypdfium2 (no preexec_fn: not
    # thread-safe, and Prefect runs tasks in threads).
    limits = [str(CHILD_AS_BYTES), str(CHILD_CPU_S), str(CHILD_FSIZE_BYTES), str(CHILD_NOFILE)]
    argv = _child_argv([*limits, *args])
    started = time.monotonic()
    try:
        # Fixed argv list (our own module, file paths as separate arguments), never a shell.
        proc = subprocess.Popen(  # noqa: S603  # nosec B603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=dict(CHILD_ENV),
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        log.error("pdf child could not start: %s", exc)
        raise UploadRejected("the PDF could not be processed") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        budget.remaining -= time.monotonic() - started
        log.warning("pdf child timed out after %.1fs (cmd=%s)", timeout, args[:1])
        if doc_bound:
            raise UploadRejected("document took too long to render") from exc
        raise UploadRejected("PDF processing timed out") from exc
    finally:
        if proc.poll() is None:
            _kill_group(proc)
    budget.remaining -= time.monotonic() - started

    try:
        result = json.loads(stdout.decode("utf-8", "replace").strip() or "null")
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
            stderr[-2000:].decode("utf-8", "replace"),
        )
        raise UploadRejected("the PDF could not be processed")
    return result


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the child's whole session (it may have spawned helpers), then reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.communicate()


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
    budget = _Budget()
    try:
        probe = _run_child(["probe", pdf], workdir, budget)
        total = probe.get("pages")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise UploadRejected("the PDF could not be processed")
        _check_range(total, page_start, page_end)
        for index in range(page_start, page_end + 1):
            out = workdir / f"page-{index:05d}.png"
            _run_child(
                ["render", pdf, str(index), str(out), repr(RENDER_DPI), str(MAX_LONG_SIDE)],
                workdir,
                budget,
            )
            data = _read_capped(out, MAX_RENDERED_PNG_BYTES)
            out.unlink()
            yield index, _decode_child_png(data)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
