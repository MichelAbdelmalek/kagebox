"""PDF child process: the only place an untrusted PDF is parsed.

Started by pipeline.ingest.sources with ``python -I -m pipeline.ingest.pdf_child`` in its own
session with a minimal environment. The first thing ``main`` does, before pypdfium2 is even
imported, is apply the resource limits it was given (no preexec_fn in the parent, which is
unsafe in a threaded worker). It never decides policy: limits arrive as arguments, and the
parent re-validates every output.

  <as> <cpu> <fsize> <nofile> probe <pdf>
      -> {"ok": true, "pages": N}
  <as> <cpu> <fsize> <nofile> render <pdf> <page> <out.png> <dpi> <max_long_side>
      -> {"ok": true}

Known failures print {"ok": false, "error": <code>} and exit 2. The parent maps codes to fixed
user-safe messages; it never shows anything the child wrote.
"""

from __future__ import annotations

import json
import resource
import sys
from pathlib import Path
from typing import Any

EXIT_REFUSED = 2
N_LIMIT_ARGS = 4


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def apply_limits(address_space: int, cpu_s: int, fsize: int, nofile: int) -> None:
    """Lower soft and hard limits on this process (never above an existing hard limit)."""
    for which, value in (
        (resource.RLIMIT_AS, address_space),
        (resource.RLIMIT_CPU, cpu_s),
        (resource.RLIMIT_FSIZE, fsize),
        (resource.RLIMIT_NOFILE, nofile),
        (resource.RLIMIT_CORE, 0),
    ):
        _, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(which, (value, value))


def _open(pdf: Path) -> Any:
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

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
    import pypdfium2 as pdfium
    from PIL import Image

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
        if max(image.size) > max_long_side:  # rounding in pdfium; the parent refuses oversize
            image.thumbnail((max_long_side, max_long_side), Image.Resampling.LANCZOS)
        image.save(out, "PNG")
        return {"ok": True}
    finally:
        doc.close()


def main(argv: list[str]) -> int:
    try:
        if len(argv) < N_LIMIT_ARGS:
            raise Refused("usage")
        try:
            limits = [int(v) for v in argv[:N_LIMIT_ARGS]]
            apply_limits(*limits)
        except (ValueError, OSError) as exc:
            raise Refused("limits") from exc
        cmd = argv[N_LIMIT_ARGS:]
        if len(cmd) == 2 and cmd[0] == "probe":
            result = probe(Path(cmd[1]))
        elif len(cmd) == 6 and cmd[0] == "render":
            result = render(Path(cmd[1]), int(cmd[2]), Path(cmd[3]), float(cmd[4]), int(cmd[5]))
        else:
            raise Refused("usage")
    except Refused as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return EXIT_REFUSED
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
