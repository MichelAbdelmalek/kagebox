"""PDF child process: the only place an untrusted PDF is parsed.

Started by pipeline.ingest.sources with ``python -I -m pipeline.ingest.pdf_child`` under
resource limits (address space, CPU, file size, open files) and a minimal environment.
It never decides policy: limits arrive as arguments, and the parent re-validates every output.

  probe  <pdf>                                          -> {"ok": true, "pages": N}
  render <pdf> <page 1-based> <out.png> <dpi> <max_long_side> -> {"ok": true}

Known failures print {"ok": false, "error": <code>} and exit 2. The parent maps codes to fixed
user-safe messages; it never shows anything the child wrote.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

EXIT_REFUSED = 2


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _open(pdf: Path) -> pdfium.PdfDocument:
    try:
        doc = pdfium.PdfDocument(pdf)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == pdfium_c.FPDF_ERR_PASSWORD:
            raise Refused("encrypted") from exc
        raise Refused("invalid") from exc
    # Encrypted with an empty user password still opens: refuse it all the same.
    if pdfium_c.FPDF_GetSecurityHandlerRevision(doc.raw) != -1:
        doc.close()
        raise Refused("encrypted")
    return doc


def probe(pdf: Path) -> dict[str, object]:
    doc = _open(pdf)
    try:
        return {"ok": True, "pages": len(doc)}
    finally:
        doc.close()


def render(pdf: Path, page_no: int, out: Path, dpi: float, max_long_side: int) -> dict[str, object]:
    doc = _open(pdf)
    try:
        if not 1 <= page_no <= len(doc):
            raise Refused("range")
        page = doc[page_no - 1]
        try:
            width, height = page.get_size()
            if not (width > 0 and height > 0):
                raise Refused("page")
            scale = min(dpi / 72.0, max_long_side / max(width, height))
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        except pdfium.PdfiumError as exc:
            raise Refused("page") from exc
        finally:
            page.close()
        image.save(out, "PNG")
        return {"ok": True}
    finally:
        doc.close()


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "probe":
            result = probe(Path(argv[1]))
        elif len(argv) == 6 and argv[0] == "render":
            result = render(
                Path(argv[1]), int(argv[2]), Path(argv[3]), float(argv[4]), int(argv[5])
            )
        else:
            raise Refused("usage")
    except Refused as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return EXIT_REFUSED
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
