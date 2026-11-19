from __future__ import annotations

import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageFile

from pipeline.common.uploads import UploadRejected
from pipeline.ingest import sources
from pipeline.ingest.sources import detect_kind, iter_pages


def make_pdf(path: Path, pages: int, size: tuple[float, float] = (595, 842)) -> Path:
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(*size)
    with path.open("wb") as fh:
        doc.save(fh)
    doc.close()
    return path


def _raw_pdf(objs: list[bytes], trailer_extra: bytes = b"") -> bytes:
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<</Size %d /Root 1 0 R" % (len(objs) + 1) + trailer_extra + b">>\n")
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


def make_encrypted_pdf(path: Path) -> Path:
    """A PDF whose trailer carries a Standard security handler (user password unknown)."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>",
        b"<</Filter/Standard/V 1/R 2/Length 40/P -4/O<"
        + b"11" * 32
        + b">/U<"
        + b"22" * 32
        + b">>>",
    ]
    file_id = b"<0123456789abcdef0123456789abcdef>"
    path.write_bytes(_raw_pdf(objs, b" /Encrypt 4 0 R /ID[" + file_id + file_id + b"]"))
    return path


def png_bytes(size: tuple[int, int], color: str = "white") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def test_pdf_pages_in_range(tmp_path: Path) -> None:
    # 2000 x 3000 pt would be 12500 px tall at 300 dpi: the long side must be capped.
    pdf = make_pdf(tmp_path / "book.pdf", 5, size=(2000, 3000))
    pages = list(iter_pages(pdf, "pdf", 2, 4))
    assert [i for i, _ in pages] == [2, 3, 4]
    for _, im in pages:
        assert im.mode == "RGB"
        assert max(im.size) <= 3000


def test_pdf_small_page_rendered(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "a5.pdf", 1, size=(420, 595))
    [(idx, im)] = list(iter_pages(pdf, "pdf", 1, 1))
    assert idx == 1
    assert 1000 < max(im.size) <= 3000


def test_cbz_natural_sort(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # width encodes the expected order
        zf.writestr("ch1/10.png", png_bytes((30, 40)))
        zf.writestr("ch1/2.png", png_bytes((20, 40)))
        zf.writestr("ch1/1.png", png_bytes((10, 40)))
    cbz = tmp_path / "x.cbz"
    cbz.write_bytes(buf.getvalue())
    pages = list(iter_pages(cbz, "cbz", 1, 3))
    assert [i for i, _ in pages] == [1, 2, 3]
    assert [im.size[0] for _, im in pages] == [10, 20, 30]
    assert all(im.mode == "RGB" for _, im in pages)


def test_cbz_non_image_entries_skipped_and_big_page_downscaled(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ComicInfo.xml", b"<ComicInfo/>")
        zf.writestr("001.png", png_bytes((1000, 4000)))
    cbz = tmp_path / "x.cbz"
    cbz.write_bytes(buf.getvalue())
    [(idx, im)] = list(iter_pages(cbz, "cbz", 1, 1))
    assert idx == 1
    assert im.size == (750, 3000)


def test_images_folder(tmp_path: Path) -> None:
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "p10.png").write_bytes(png_bytes((30, 40)))
    (folder / "p2.png").write_bytes(png_bytes((20, 40)))
    (folder / "p1.png").write_bytes(png_bytes((10, 40)))
    (folder / "notes.txt").write_text("not an image")
    (folder / "sub").mkdir()
    (folder / "link.png").symlink_to(folder / "p1.png")
    pages = list(iter_pages(folder, "images", 2, 3))
    assert [(i, im.size[0]) for i, im in pages] == [(2, 20), (3, 30)]


def test_detect_kind(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "named.cbz", 1)  # the name lies; content wins
    assert detect_kind(pdf) == "pdf"
    cbz = tmp_path / "named.pdf"
    with zipfile.ZipFile(cbz, "w") as zf:
        zf.writestr("1.png", png_bytes((4, 4)))
    assert detect_kind(cbz) == "cbz"
    assert detect_kind(tmp_path) == "images"
    other = tmp_path / "x.bin"
    other.write_bytes(b"MZ\x90\x00 not a page source")
    with pytest.raises(UploadRejected):
        detect_kind(other)


def test_encrypted_pdf_refused(tmp_path: Path) -> None:
    pdf = make_encrypted_pdf(tmp_path / "locked.pdf")
    with pytest.raises(UploadRejected, match="encrypted"):
        list(iter_pages(pdf, "pdf", 1, 1))


def test_too_many_pages_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "MAX_PAGES", 3)
    pdf = make_pdf(tmp_path / "long.pdf", 5)
    with pytest.raises(UploadRejected, match="too many pages"):
        list(iter_pages(pdf, "pdf", 1, 1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in range(4):
            zf.writestr(f"{n}.png", png_bytes((4, 4)))
    cbz = tmp_path / "long.cbz"
    cbz.write_bytes(buf.getvalue())
    with pytest.raises(UploadRejected, match="too many pages"):
        list(iter_pages(cbz, "cbz", 1, 1))


def test_child_crash_is_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf(tmp_path / "ok.pdf", 1)
    crash = "import sys; sys.stderr.write('boom'); sys.exit(3)"
    monkeypatch.setattr(sources, "_child_argv", lambda args: [sys.executable, "-I", "-c", crash])
    with pytest.raises(UploadRejected) as exc:
        list(iter_pages(pdf, "pdf", 1, 1))
    assert "boom" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_child_hang_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf(tmp_path / "ok.pdf", 1)
    monkeypatch.setattr(sources, "CHILD_TIMEOUT_S", 1.0)
    monkeypatch.setattr(
        sources,
        "_child_argv",
        lambda args: [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
    )
    start = time.monotonic()
    with pytest.raises(UploadRejected, match="timed out"):
        list(iter_pages(pdf, "pdf", 1, 1))
    assert time.monotonic() - start < 10


def test_child_bad_manifest_is_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf(tmp_path / "ok.pdf", 1)
    monkeypatch.setattr(
        sources, "_child_argv", lambda args: [sys.executable, "-I", "-c", "print('not json')"]
    )
    with pytest.raises(UploadRejected):
        list(iter_pages(pdf, "pdf", 1, 1))


def test_corrupt_pdf_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\ngarbage that is not a pdf")
    with pytest.raises(UploadRejected):
        list(iter_pages(bad, "pdf", 1, 1))


@pytest.mark.parametrize(("start", "end"), [(0, 1), (2, 1), (1, 6), (6, 6)])
def test_range_out_of_bounds_refused(tmp_path: Path, start: int, end: int) -> None:
    pdf = make_pdf(tmp_path / "five.pdf", 5)
    with pytest.raises(UploadRejected, match="page range"):
        list(iter_pages(pdf, "pdf", start, end))


def test_temp_dir_removed_on_early_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Path] = []
    real = sources.tempfile.mkdtemp

    def spy(*a: object, **k: object) -> str:
        d = real(prefix="kagebox-pdf-", dir=tmp_path)
        created.append(Path(d))
        return d

    monkeypatch.setattr(sources.tempfile, "mkdtemp", spy)
    pdf = make_pdf(tmp_path / "five.pdf", 5)
    gen = iter_pages(pdf, "pdf", 1, 5)
    next(gen)
    gen.close()
    assert created
    assert not any(d.exists() for d in created)


def test_child_runs_with_rlimits_and_minimal_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real pdf_child entry point applies all five limits to itself (no preexec_fn)."""
    monkeypatch.setenv("TOGETHER_API_KEY", "secret-should-not-leak")
    monkeypatch.setattr(sources, "CHILD_NOFILE", 48)
    script = (
        "import json, os, sys, resource as r\n"
        "from pipeline.ingest import pdf_child\n"
        "def fake_probe(pdf):\n"
        "    names = ['AS', 'CPU', 'FSIZE', 'NOFILE', 'CORE']\n"
        "    lim = {n: list(r.getrlimit(getattr(r, 'RLIMIT_' + n))) for n in names}\n"
        "    return {'ok': True, 'lim': lim, 'env': sorted(os.environ),\n"
        "            'pdfium_loaded': 'pypdfium2' in sys.modules}\n"
        "pdf_child.probe = fake_probe\n"
        "sys.exit(pdf_child.main(sys.argv[1:]))\n"
    )
    monkeypatch.setattr(
        sources, "_child_argv", lambda args: [sys.executable, "-I", "-c", script, *args]
    )
    out = sources._run_child(["probe", "x"], tmp_path)
    lim = out["lim"]
    assert lim == {
        "AS": [2 * 1024**3] * 2,
        "CPU": [60, 60],
        "FSIZE": [sources.CHILD_FSIZE_BYTES] * 2,
        "NOFILE": [48, 48],
        "CORE": [0, 0],
    }
    assert "TOGETHER_API_KEY" not in out["env"]  # type: ignore[operator]
    assert out["pdfium_loaded"] is False


def test_run_child_does_not_use_preexec_fn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    real_popen = sources.subprocess.Popen

    def spy(*a: object, **k: object) -> object:
        seen.update(k)
        return real_popen(*a, **k)  # type: ignore[call-overload]

    monkeypatch.setattr(sources.subprocess, "Popen", spy)
    ok = "print('{\"ok\": true}')"
    monkeypatch.setattr(sources, "_child_argv", lambda args: [sys.executable, "-I", "-c", ok])
    sources._run_child(["probe", "x"], tmp_path)
    assert seen.get("preexec_fn") is None
    assert seen.get("start_new_session") is True


# --- fix round 1 ---------------------------------------------------------------------------

FAKE_CHILD = """
import json, sys, time
args = sys.argv[1:]
time.sleep(float({sleep!r}))
if "probe" in args:
    print(json.dumps({{"ok": True, "pages": {pages}}}))
else:
    from PIL import Image
    i = args.index("render")
    Image.new("RGB", ({side}, {side}), "white").save(args[i + 3], "PNG")
    print(json.dumps({{"ok": True}}))
"""


def fake_child(
    monkeypatch: pytest.MonkeyPatch, *, sleep: float = 0, pages: int = 3, side: int = 50
) -> None:
    script = FAKE_CHILD.format(sleep=sleep, pages=pages, side=side)
    monkeypatch.setattr(
        sources, "_child_argv", lambda args: [sys.executable, "-I", "-c", script, *args]
    )


def cbz_of(tmp_path: Path, entries: list[tuple[str, bytes]]) -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    path = tmp_path / "x.cbz"
    path.write_bytes(buf.getvalue())
    return path


def img_bytes(fmt: str, size: tuple[int, int] = (8, 8)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
def test_cbz_unsupported_image_format_refused_not_skipped(tmp_path: Path, fmt: str) -> None:
    cbz = cbz_of(
        tmp_path,
        [
            ("1.png", png_bytes((4, 4))),
            (f"2.{fmt.lower()}", img_bytes(fmt)),
            ("3.png", png_bytes((4, 4))),
        ],
    )
    with pytest.raises(UploadRejected, match="unsupported image format"):
        list(iter_pages(cbz, "cbz", 1, 1))


def test_images_folder_unsupported_image_format_refused(tmp_path: Path) -> None:
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "1.png").write_bytes(png_bytes((4, 4)))
    (folder / "2.bmp").write_bytes(img_bytes("BMP"))
    with pytest.raises(UploadRejected, match="unsupported image format"):
        list(iter_pages(folder, "images", 1, 1))


def test_natural_key_hostile_names() -> None:
    names = ["a\u00b2.png", "a10.png", "a2.png", "9" * 5000 + ".png", "0099.png", "10.png"]
    ordered = sorted(names, key=sources._natural_key)
    assert ordered.index("a2.png") < ordered.index("a10.png")
    assert ordered.index("10.png") < ordered.index("0099.png") < ordered.index("9" * 5000 + ".png")


def test_cbz_hostile_names_sort(tmp_path: Path) -> None:
    cbz = cbz_of(
        tmp_path,
        [("p\u00b2.png", png_bytes((10, 4))), ("1" * 5000 + ".png", png_bytes((20, 4)))],
    )
    pages = list(iter_pages(cbz, "cbz", 1, 2))
    assert [im.size[0] for _, im in pages] == [20, 10]


class Blocked(BaseException):
    """Not an OSError, so code under test cannot swallow it as an open() failure."""


def _run_with_alarm(fn: object, seconds: int = 3) -> None:
    import signal

    def boom(*_: object) -> None:
        raise Blocked("blocked on a FIFO")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        fn()  # type: ignore[operator]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("kind", ["detect", "pdf", "cbz"])
def test_fifo_refused_promptly(tmp_path: Path, kind: str) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    def go() -> None:
        with pytest.raises(UploadRejected):
            if kind == "detect":
                detect_kind(fifo)
            else:
                list(iter_pages(fifo, kind, 1, 1))  # type: ignore[arg-type]

    _run_with_alarm(go)


def test_read_capped_fifo_refused_promptly(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    def go() -> None:
        with pytest.raises(UploadRejected, match="regular file"):
            sources._read_capped(fifo, 100)

    _run_with_alarm(go)


def test_cbz_pixel_cap_is_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1600 px is between the cap and 2x the cap, where Pillow only warns.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    cbz = cbz_of(tmp_path, [("1.png", png_bytes((40, 40)))])
    with pytest.raises(UploadRejected, match="too large"):
        list(iter_pages(cbz, "cbz", 1, 1))


def test_images_folder_pixel_cap_is_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "1.png").write_bytes(png_bytes((40, 40)))
    with pytest.raises(UploadRejected, match="too large"):
        list(iter_pages(folder, "images", 1, 1))


def test_decode_image_checks_size_before_load(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.common import uploads

    data = png_bytes((40, 40))  # built before the spy: saving calls load()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    loaded: list[bool] = []
    real_load = Image.Image.load

    def spy(self: Image.Image) -> object:
        loaded.append(True)
        return real_load(self)

    monkeypatch.setattr(Image.Image, "load", spy)
    monkeypatch.setattr(ImageFile.ImageFile, "load", spy)
    with pytest.raises(UploadRejected, match="too large"):
        uploads.decode_image(data, uploads.IMAGE_MIMES, 10_000)
    assert not loaded


def test_child_png_over_long_side_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf(tmp_path / "ok.pdf", 3)
    fake_child(monkeypatch, side=50)
    monkeypatch.setattr(sources, "MAX_LONG_SIDE", 10)
    with pytest.raises(UploadRejected, match="could not be decoded safely"):
        list(iter_pages(pdf, "pdf", 1, 1))


def test_document_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf(tmp_path / "ok.pdf", 1)
    fake_child(monkeypatch, sleep=0.3, pages=20)
    monkeypatch.setattr(sources, "DOC_DEADLINE_S", 1.5)
    got: list[int] = []
    start = time.monotonic()
    with pytest.raises(UploadRejected, match="document took too long to render"):
        for index, _ in iter_pages(pdf, "pdf", 1, 20):
            got.append(index)
    assert 0 < len(got) < 20
    assert time.monotonic() - start < 6


def test_timeout_kills_process_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "grandchild-alive"
    grandchild = f"import time; time.sleep(2); open({str(marker)!r}, 'w').close()"
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(sources, "CHILD_TIMEOUT_S", 0.5)
    monkeypatch.setattr(sources, "_child_argv", lambda args: [sys.executable, "-I", "-c", script])
    with pytest.raises(UploadRejected, match="timed out"):
        sources._run_child(["probe", "x"], tmp_path)
    time.sleep(2.5)
    assert not marker.exists()


def test_fit_width_keeps_tall_strip_height(tmp_path: Path) -> None:
    folder = tmp_path / "strip"
    folder.mkdir()
    (folder / "001.png").write_bytes(png_bytes((800, 30000)))
    (folder / "002.png").write_bytes(png_bytes((4000, 9000)))
    pages = dict(iter_pages(folder, "images", 1, 2, fit="width"))
    assert pages[1].size == (800, 30000)  # untouched: width already within the cap
    assert pages[2].size == (3000, 6750)  # only the width is capped, aspect kept
    [(_, default)] = list(iter_pages(folder, "images", 1, 1))
    assert default.size == (80, 3000)  # default stays long-side capped


def test_fit_width_cbz(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("001.png", png_bytes((1000, 8000)))
    cbz = tmp_path / "s.cbz"
    cbz.write_bytes(buf.getvalue())
    [(_, im)] = list(iter_pages(cbz, "cbz", 1, 1, fit="width"))
    assert im.size == (1000, 8000)


def test_fit_width_pdf_refused(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "book.pdf", 1)
    with pytest.raises(UploadRejected, match="CBZ or an image folder"):
        list(iter_pages(pdf, "pdf", 1, 1, fit="width"))


def test_fit_unknown_refused(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "book.pdf", 1)
    with pytest.raises(ValueError):
        iter_pages(pdf, "pdf", 1, 1, fit="height")  # type: ignore[arg-type]
