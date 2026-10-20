"""Adapter HILO (pdf_providers/hilo.py) — không cần mạng, không nạp ddddocr/skia.

Run::

    pytest tests/test_pdf_hilo.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common, hilo     # noqa: E402

GRAB = {"shdon": 2096065, "khhdon": "C26TAA", "khmshdon": 1, "nbmst": "0312650437",
        "nbten": "CÔNG TY TNHH GRAB", "lookup_url": "https://vn.einvoice.grab.com",
        "ma_tra_cuu": "2c70cfd036bf4fb0b0514dd4d425aef1",
        "tt_khac": {"SearchKey": "2c70cfd036bf4fb0b0514dd4d425aef1", "Fkey": "26AAgwlx1vpbf1c",
                    "MappingKey": "2096065"}}
GSM = {"shdon": 75135, "khhdon": "C26TBB", "nbmst": "0110269067", "lookup_url": "",
       "tt_khac": {"Hilo-SearchKey": "01M3P9ZVAR6X21HW83GKJ18T6R"}}

HOME = '<form><input name="__RequestVerificationToken" type="hidden" value="TOK1" /></form>'
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="250" height="60" viewBox="0,0,250,60">'
       "<path d='M0 57 C56 52, 190 31, 250 16' stroke='#00B14F' fill='none' stroke-width='0.5'/>"
       '<path fill="#00B14F" d="M 10 10 L 20 20 Z" stroke="#00B14F"/>'
       '<path fill="#00B14F" d="M 60 10 L 70 20 Z" stroke="#00B14F"/>'
       "<path d='M0 17 C35 27, 20 51, 250 52' stroke='#00B14F' fill='none' stroke-width='1.4'/>"
       '<path fill="#00B14F" d="M 110 10 L 120 20 Z" stroke="#00B14F"/>'
       '<path fill="#00B14F" d="M 160 10 L 170 20 Z" stroke="#00B14F"/></svg>')
FOUND = ('<tr id="26AAgwlx1vpbf1c" pattern="1" serial="C26TAA">'
         '<a download-file="/Invoice/DowloadXml?Fkey=26AAgwlx1vpbf1c">x</a>'
         '<a download-file="/Invoice/DowloadPdf?Fkey=26AAgwlx1vpbf1c">x</a></tr>')
WRONG = '<div flash-message="Sai m&amp;#227; kiểm tra." flash-message-type="error"></div>'
NOT_FOUND = ('<div flash-message="Kh&amp;#244;ng t&amp;#236;m thấy h&amp;#243;a đơn" '
             'flash-message-type="error"></div>')


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):
        page.insert_text((40, 40 + i * 18), f"{text} dong {i}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


class _Resp:
    def __init__(self, status=200, content=b"", text="", headers=None, url=""):
        self.status_code, self.content, self.text = status, content, text
        self.headers, self.url = headers or {}, url


class _FakeOcr:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.seen = []

    def classification(self, png):
        self.seen.append(png)
        return self.answers.pop(0)


@pytest.fixture
def ocr(monkeypatch):
    """OCR giả + không dựng PNG thật (skia); ghi lại SVG đã đưa vào để kiểm bỏ nhiễu."""
    fake = _FakeOcr()
    monkeypatch.setattr(hilo, "_ocr", lambda: fake)
    monkeypatch.setattr(hilo, "_svg_to_png", lambda svg, scale=2.0: svg.encode())
    return fake


def _install(monkeypatch, results, pdf, calls):
    """Giả cổng Grab: trang form → captcha → kết quả (chuỗi, hoặc list trả lần lượt) → PDF."""
    home = _Resp(text=HOME)

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if "/hoa-don/tra-cuu" in url:
            if not kw.get("params"):
                return home
            nxt = results.pop(0) if isinstance(results, list) else results
            return _Resp(text=nxt, url=url + "?q")
        if "/Captcha/Show" in url:
            return _Resp(text=SVG)
        if "/Invoice/DowloadPdf" in url:
            return _Resp(content=pdf, headers={"Content-Type": "application/pdf"})
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(hilo, "request", fake)
    monkeypatch.setattr(hilo, "new_session", lambda: object())
    monkeypatch.setattr(hilo.time, "sleep", lambda s: None)


# --- captcha SVG -------------------------------------------------------------

def test_glyph_svg_drops_noise_lines():
    clean, n = hilo._glyph_svg(SVG)
    assert n == 4
    assert "fill='none'" not in clean and clean.count("<path") == 4
    assert 'width="250" height="60"' in clean


def test_solve_svg_captcha_uppercases(ocr):
    ocr.answers = ["ab3d"]
    assert hilo.solve_svg_captcha(SVG) == "AB3D"
    assert b"fill='none'" not in ocr.seen[0]                # OCR chỉ thấy ký tự, không thấy nhiễu


@pytest.mark.parametrize("read", ["AB3", "AB3DE", "", "A-B"])
def test_solve_svg_captcha_refuses_wrong_length(ocr, read):
    ocr.answers = [read]
    with pytest.raises(hilo._WrongCaptcha):
        hilo.solve_svg_captcha(SVG)                           # 4 path ký tự → phải đọc đủ 4


def test_solve_svg_captcha_not_svg(ocr):
    with pytest.raises(hilo._WrongCaptcha):
        hilo.solve_svg_captcha("<html>lỗi</html>")


# --- định tuyến -------------------------------------------------------------

def test_portal_mapping():
    assert hilo.HiloAdapter.portal_for(GRAB) == "https://vn.einvoice.grab.com"
    assert hilo.HiloAdapter.portal_for({"nbmst": "0312650437"}) == "https://vn.einvoice.grab.com"
    assert hilo.HiloAdapter.portal_for(
        {"nbmst": "0999999999", "lookup_url": "https://VN.einvoice.grab.com/"}) == "https://vn.einvoice.grab.com"
    assert hilo.HiloAdapter.portal_for(GSM) is None
    assert hilo.HiloAdapter.portal_for({"nbmst": "1", "lookup_url": "https://la.example.com"}) is None


def test_unknown_seller_parks_as_no_portal(monkeypatch):
    calls = []
    _install(monkeypatch, FOUND, b"", calls)
    with pytest.raises(PdfFetchError) as ei:
        hilo.HiloAdapter().fetch_pdf(GSM)
    assert ei.value.kind == "no_portal" and not calls       # không gọi mạng


def test_grab_without_fkey_is_no_code():
    inv = dict(GRAB, tt_khac={"SearchKey": "2c70cfd036bf4fb0b0514dd4d425aef1"})
    with pytest.raises(PdfFetchError) as ei:
        hilo.HiloAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_registry():
    assert pdf_providers.get_adapter("hilo").mst_tcgp == ("0106713804",)
    assert pdf_providers.adapter_for_mst_tcgp("0106713804").source_id == "hilo"


# --- luồng tải ----------------------------------------------------------------

def test_ok_searches_by_fkey_and_downloads(monkeypatch, ocr):
    ocr.answers = ["wydm"]
    calls = []
    _install(monkeypatch, FOUND, _pdf_with("So 2096065"), calls)
    assert hilo.HiloAdapter().fetch_pdf(GRAB).startswith(b"%PDF")
    search = [kw["params"] for m, u, kw in calls if kw.get("params")]
    assert search == [{"IsFirstGet": "False", "__RequestVerificationToken": "TOK1",
                       "key": "26AAgwlx1vpbf1c", "captch": "WYDM"}]   # Fkey, KHÔNG phải SearchKey
    assert calls[-1][1] == "https://vn.einvoice.grab.com/Invoice/DowloadPdf?Fkey=26AAgwlx1vpbf1c"


def test_retries_wrong_captcha_then_succeeds(monkeypatch, ocr):
    ocr.answers = ["AAAA", "BBBB"]
    calls = []
    _install(monkeypatch, [WRONG, FOUND], _pdf_with("So 2096065"), calls)
    assert hilo.HiloAdapter().fetch_pdf(GRAB).startswith(b"%PDF")
    assert [kw["params"]["captch"] for _, _, kw in calls if kw.get("params")] == ["AAAA", "BBBB"]


def test_misread_length_fetches_new_captcha_without_submitting(monkeypatch, ocr):
    ocr.answers = ["AB3", "AB3D"]
    calls = []
    _install(monkeypatch, FOUND, _pdf_with("So 2096065"), calls)
    hilo.HiloAdapter().fetch_pdf(GRAB)
    assert [kw["params"]["captch"] for _, _, kw in calls if kw.get("params")] == ["AB3D"]


def test_gives_up_on_captcha(monkeypatch, ocr):
    ocr.answers = ["AAAA"] * hilo.CAPTCHA_TRIES
    _install(monkeypatch, WRONG, b"", [])
    with pytest.raises(PdfFetchError) as ei:
        hilo.HiloAdapter().fetch_pdf(GRAB)
    assert ei.value.kind == "captcha"


@pytest.mark.parametrize("page, pdf, kind", [
    (NOT_FOUND, b"", "no_pdf"),
    ("<html>trang lạ</html>", b"", "bad_content"),                         # không có link PDF
    (FOUND, b"<html>" + b"x" * 2000, "bad_content"),                       # trả HTML thay PDF
    (FOUND, _pdf_with("So 9999999"), "bad_content"),                       # PDF của HĐ khác
])
def test_errors(monkeypatch, ocr, page, pdf, kind):
    ocr.answers = ["ABCD"]
    _install(monkeypatch, page, pdf, [])
    with pytest.raises(PdfFetchError) as ei:
        hilo.HiloAdapter().fetch_pdf(GRAB)
    assert ei.value.kind == kind
