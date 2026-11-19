from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image

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
    monkeypatch.setenv("TOGETHER_API_KEY", "secret-should-not-leak")
    script = (
        "import json, os, resource as r; print(json.dumps({'ok': True,"
        " 'as': r.getrlimit(r.RLIMIT_AS)[0], 'cpu': r.getrlimit(r.RLIMIT_CPU)[0],"
        " 'nofile': r.getrlimit(r.RLIMIT_NOFILE)[0], 'env': sorted(os.environ)}))"
    )
    monkeypatch.setattr(sources, "_child_argv", lambda args: [sys.executable, "-I", "-c", script])
    out = sources._run_child(["probe", "x"], tmp_path)
    assert out["as"] == 2 * 1024**3
    assert out["cpu"] == 60
    assert out["nofile"] == sources.CHILD_NOFILE
    assert "TOGETHER_API_KEY" not in out["env"]  # type: ignore[operator]
