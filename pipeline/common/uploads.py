"""Upload validation: size limits, type sniffing, image re-encoding, safe archive reading.

Everything that arrives from a user is untrusted. Nothing here trusts a file name, an extension
or a declared content type.
"""

from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import filetype
from PIL import Image

Image.MAX_IMAGE_PIXELS = 120_000_000  # decompression-bomb guard (tall manhwa strips fit)

IMAGE_MIMES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
DOC_MIMES = {"application/pdf": "pdf", **IMAGE_MIMES}
ARCHIVE_MIMES = {"application/zip": "cbz"}


class UploadRejected(ValueError):
    """The upload failed validation. The message is safe to show to the user."""


@dataclass(frozen=True)
class Sniffed:
    mime: str
    ext: str


def sniff(data: bytes, allowed: dict[str, str], max_bytes: int) -> Sniffed:
    if not data:
        raise UploadRejected("empty file")
    if len(data) > max_bytes:
        raise UploadRejected(f"file is larger than {max_bytes} bytes")
    kind = filetype.guess(data)
    if kind is None or kind.mime not in allowed:
        raise UploadRejected("file type not allowed")
    return Sniffed(kind.mime, allowed[kind.mime])


_PIL_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_DECODE_ERRORS = (Image.DecompressionBombError, OSError, SyntaxError, ValueError)


def _open_checked(data: bytes, formats: tuple[str, ...]) -> Image.Image:
    """Open (header only), refuse more than Image.MAX_IMAGE_PIXELS strictly, then decode.

    Pillow only warns between MAX_IMAGE_PIXELS and twice that; we refuse before any pixel data
    is decoded so a small file cannot claim a huge canvas.
    """
    try:
        im = Image.open(io.BytesIO(data), formats=formats)
    except _DECODE_ERRORS as exc:
        raise UploadRejected("image could not be decoded safely") from exc
    width, height = im.size
    cap = Image.MAX_IMAGE_PIXELS
    if cap is not None and width * height > cap:
        im.close()
        raise UploadRejected("image is too large")
    try:
        im.load()
    except _DECODE_ERRORS as exc:
        im.close()
        raise UploadRejected("image could not be decoded safely") from exc
    return im


def open_image_checked(data: bytes, formats: tuple[str, ...]) -> Image.Image:
    """Public form of the strict guard: allowed formats only, header size refused before decode."""
    return _open_checked(data, formats)


def decode_image(data: bytes, allowed: dict[str, str], max_bytes: int) -> Image.Image:
    """Sniff, size-check and decode once; returns the decoded image (metadata is not kept)."""
    sniffed = sniff(data, allowed, max_bytes)
    fmt = _PIL_FORMATS.get(sniffed.mime)
    if fmt is None:
        raise UploadRejected("file type not allowed")
    return _open_checked(data, (fmt,))


def reencode_image(data: bytes) -> bytes:
    """Decode and re-encode as PNG: drops metadata and anything appended (polyglots)."""
    with _open_checked(data, tuple(_PIL_FORMATS.values())) as im:
        out = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
        return out.getvalue()


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 2_000
    max_total_bytes: int = 2 * 1024**3
    max_entry_bytes: int = 200 * 1024**2
    max_ratio: float = 100.0


def safe_read_zip(data: bytes, limits: ArchiveLimits | None = None) -> list[tuple[str, bytes]]:
    """Return (normalized name, bytes) for each regular file, refusing zip-slip and zip bombs.

    Never writes to disk; callers store the content-addressed bytes in the object store.
    """
    lim = limits or ArchiveLimits()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadRejected("not a valid archive") from exc
    infos = zf.infolist()
    if len(infos) > lim.max_entries:
        raise UploadRejected("archive has too many entries")
    if sum(i.file_size for i in infos) > lim.max_total_bytes:
        raise UploadRejected("archive is too large when unpacked")
    out: list[tuple[str, bytes]] = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        unsafe = (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or ":" in path.parts[0]  # Windows drive letters
            or "\x00" in name
        )
        if unsafe:
            raise UploadRejected("archive entry has an unsafe path")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise UploadRejected("archive contains a symbolic link")
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            raise UploadRejected("encrypted archives are not supported")
        if info.file_size > lim.max_entry_bytes:
            raise UploadRejected("archive entry is too large")
        if info.compress_size and info.file_size / info.compress_size > lim.max_ratio:
            raise UploadRejected("archive entry compression ratio is suspicious")
        with zf.open(info) as fh:
            content = fh.read(lim.max_entry_bytes + 1)
        if len(content) > lim.max_entry_bytes:  # header lied about size
            raise UploadRejected("archive entry is too large")
        out.append((str(path), content))
    return out
