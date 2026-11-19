"""Wrap a terms snapshot (HTML) in a PDF so the rights endpoint accepts it as evidence.

The rights endpoint takes PDF or images only. This writes a one-page PDF (standard Helvetica,
ASCII text: what the snapshot is, its date, size and sha256) with the snapshot bytes attached
UNCHANGED as an embedded file, so the evidence stays verifiable against its recorded sha256.
Standard library only; no rendering, no network.

    python3 -I tests/e2e/evidence_pdf.py SNAPSHOT.html OUT.pdf "URL" "YYYY-MM-DD"
"""

from __future__ import annotations

import hashlib
import sys
import zlib
from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build(snapshot: bytes, name: str, url: str, date: str) -> bytes:
    digest = hashlib.sha256(snapshot).hexdigest()
    lines = [
        "Kagebox rights evidence: terms snapshot",
        f"Source: {url}",
        f"Snapshot taken: {date}",
        f"Attached file: {name} ({len(snapshot)} bytes)",
        f"sha256: {digest}",
        "The snapshot is attached to this PDF unchanged (embedded file).",
        "Its text is the rights holder's terms; Japanese text controls.",
    ]
    text = "BT /F1 11 Tf 50 780 Td 15 TL " + " ".join(f"({_esc(t)}) Tj T*" for t in lines) + " ET"
    content = text.encode("ascii")
    packed = zlib.compress(snapshot)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [("
        + name.encode("ascii")
        + b") 6 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Filespec /F ("
        + name.encode("ascii")
        + b") /UF ("
        + name.encode("ascii")
        + b") /EF << /F 7 0 R >> >>",
        b"<< /Type /EmbeddedFile /Subtype /text#2Fhtml /Filter /FlateDecode "
        b"/Params << /Size %d >> /Length %d >>\nstream\n"
        % (len(snapshot), len(packed))
        + packed
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def main() -> None:
    src, dst, url, date = sys.argv[1:5]
    path = Path(src)
    if not path.name.isascii() or "(" in path.name or ")" in path.name:
        raise SystemExit("snapshot file name must be plain ASCII")
    Path(dst).write_bytes(build(path.read_bytes(), path.name, url, date))


if __name__ == "__main__":
    main()
