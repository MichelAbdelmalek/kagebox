from __future__ import annotations

import io
import stat
import zipfile

import pytest
from PIL import Image

from pipeline.common.uploads import (
    IMAGE_MIMES,
    ArchiveLimits,
    UploadRejected,
    reencode_image,
    safe_read_zip,
    sniff,
)


def zip_of(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def png(size: tuple[int, int] = (4, 4)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "PNG")
    return out.getvalue()


@pytest.mark.parametrize("name", ["../evil.png", "/etc/passwd", "a/../../b.png", "C:/x.png"])
def test_zip_slip_rejected(name: str) -> None:
    with pytest.raises(UploadRejected, match="unsafe path"):
        safe_read_zip(zip_of([(name, b"x")]))


def test_symlink_rejected() -> None:
    info = zipfile.ZipInfo("link.png")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(UploadRejected, match="symbolic link"):
        safe_read_zip(zip_of([(info, b"/etc/passwd")]))


def test_zip_bomb_ratio_rejected() -> None:
    with pytest.raises(UploadRejected, match="ratio"):
        safe_read_zip(zip_of([("p.png", b"\0" * 5_000_000)]))


def test_entry_count_limit() -> None:
    data = zip_of([(f"{i}.png", b"x") for i in range(5)])
    with pytest.raises(UploadRejected, match="too many"):
        safe_read_zip(data, ArchiveLimits(max_entries=4))


def test_valid_archive_reads() -> None:
    p = png()
    assert safe_read_zip(zip_of([("ch1/001.png", p)])) == [("ch1/001.png", p)]


def test_sniff_ignores_claimed_type() -> None:
    with pytest.raises(UploadRejected, match="not allowed"):
        sniff(b"%PDF-1.7\n...", IMAGE_MIMES, 1000)
    assert sniff(png(), IMAGE_MIMES, 10_000).ext == "png"
    with pytest.raises(UploadRejected, match="larger"):
        sniff(png(), IMAGE_MIMES, 10)


def test_reencode_strips_appended_payload() -> None:
    polyglot = png() + b"<script>alert(1)</script>"
    out = reencode_image(polyglot)
    assert b"<script>" not in out
    with pytest.raises(UploadRejected):
        reencode_image(b"\x89PNG\r\n\x1a\n garbage")
