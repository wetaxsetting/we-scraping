"""PDF "bản của Thuế" dựng từ invoice.html trong ZIP export-xml của GDT. Không gọi GDT, không chạm DB thật."""
import io
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

import gdt_pdf  # noqa: E402
import invoice_job  # noqa: E402

HTML = """<html><head><meta charset="utf-8"><script src="details.js"></script>
<style>@page{size:A4;margin:0}</style></head>
<body><div class="main-page"><h1>HOÁ ĐƠN GIÁ TRỊ GIA TĂNG</h1><p id="n">Số: 00000004</p>
<img src="sign-check.jpg"><img src="https://example.invalid/track.png"></div></body></html>"""


def _zip(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def gdt_zip(html=HTML) -> bytes:
    return _zip({"invoice.xml": "<HDon/>", "invoice.html": html, "details.js": "document.title='x';",
                 "sign-check.jpg": b"\xff\xd8\xff", "viewinvoice-bg.jpg": b"\xff\xd8\xff"})


def test_has_view():
    assert gdt_pdf.has_view(gdt_zip())
    assert not gdt_pdf.has_view(_zip({"invoice.xml": "<HDon/>"}))
    assert not gdt_pdf.has_view(b"not a zip")


def test_missing_page_or_bad_zip_raises_without_browser(monkeypatch):
    monkeypatch.setattr(gdt_pdf._renderer, "render", lambda p: pytest.fail("không được mở Chromium"))
    with pytest.raises(gdt_pdf.GdtPdfError, match="invoice.html"):
        gdt_pdf.render(_zip({"invoice.xml": "<HDon/>"}))
    with pytest.raises(gdt_pdf.GdtPdfError, match="ZIP hỏng"):
        gdt_pdf.render(b"PK-broken")


def test_only_known_flat_names_are_extracted(tmp_path):
    data = _zip({"invoice.html": HTML, "../evil.html": "x", "sub/details.js": "x", "other.exe": "x"})
    page = gdt_pdf._extract(data, tmp_path)
    assert page == tmp_path / "invoice.html"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["invoice.html"]
    assert not (tmp_path.parent / "evil.html").exists()


BG_HTML = """<html><head><style>.bg-container{position:fixed;background-image:url("viewinvoice-bg.jpg")}</style></head>
<BODY class="x"><div class="main-page">HĐ</div></BODY></html>"""


def test_background_div_is_added_once_right_after_body(tmp_path):
    page = gdt_pdf._extract(gdt_zip(BG_HTML), tmp_path)
    html = page.read_text(encoding="utf-8")
    assert '<BODY class="x"><div class="bg-container"></div><div class="main-page">' in html
    gdt_pdf._add_background(page)                       # chạy lại không chèn thêm
    assert page.read_text(encoding="utf-8").count('class="bg-container"') == 1


def test_background_div_skipped_without_css_or_image(tmp_path):
    page = gdt_pdf._extract(gdt_zip(HTML), tmp_path)     # CSS không có .bg-container
    assert "bg-container" not in page.read_text(encoding="utf-8")
    other = tmp_path / "noimg"
    other.mkdir()
    page = gdt_pdf._extract(_zip({"invoice.html": BG_HTML}), other)   # thiếu ảnh nền
    assert '<div class="bg-container">' not in page.read_text(encoding="utf-8")


def test_oversized_entry_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(gdt_pdf, "MAX_ENTRY", 10)
    with pytest.raises(gdt_pdf.GdtPdfError, match="quá lớn"):
        gdt_pdf._extract(gdt_zip(), tmp_path)


def test_enabled_toggle(monkeypatch):
    monkeypatch.delenv("INVOICE_GDT_PDF", raising=False)
    assert gdt_pdf.enabled()
    monkeypatch.setenv("INVOICE_GDT_PDF", "0")
    assert not gdt_pdf.enabled()


# --- job: lưu bản của Thuế, lỗi không làm hỏng task XML ---------------------

class _Store:
    def __init__(self):
        self.saved = []

    def set_gdt_pdf(self, invoice_id, path):
        self.saved.append((invoice_id, path))


TASK = {"id": 7, "nbmst": "0312650437", "khmshdon": 1, "khhdon": "C26TWE", "shdon": 4,
        "ngay_lap": date(2026, 11, 2)}


def test_save_gdt_pdf_writes_beside_xml_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_job.gdt_pdf, "render", lambda z: b"%PDF-1.7 fake")
    store = _Store()
    rel = invoice_job.save_gdt_pdf(store, TASK, "1201496252", gdt_zip(), tmp_path)
    assert rel == "pdf_gdt/1201496252/2026/11/0312650437/1_C26TWE_4.pdf"
    assert (tmp_path / rel).read_bytes() == b"%PDF-1.7 fake"
    assert store.saved == [(7, rel)]


def test_save_gdt_pdf_failure_only_logs(tmp_path, monkeypatch):
    def boom(z):
        raise gdt_pdf.GdtPdfError("Chromium hỏng")
    monkeypatch.setattr(invoice_job.gdt_pdf, "render", boom)
    store = _Store()
    assert invoice_job.save_gdt_pdf(store, TASK, "1201496252", gdt_zip(), tmp_path) is None
    assert store.saved == [] and not (tmp_path / "pdf_gdt").exists()


# --- dựng thật bằng Chromium (bỏ qua khi máy chưa cài) -----------------------

def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _chromium_ok(), reason="chưa cài playwright chromium")
def test_real_render_is_pdf_and_offline():
    pdf = gdt_pdf.render(gdt_zip())
    try:
        assert pdf.startswith(b"%PDF-") and len(pdf) > 1000
    finally:
        gdt_pdf._renderer.close()
