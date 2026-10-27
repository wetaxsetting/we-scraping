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


# --- cổng chung cũ tracuuhddt78: link mail getinvoice?token=base64("mẫu:ký hiệu:số:MST") ---------

KDP = {"shdon": 1443, "khhdon": "C26TPD", "khmshdon": 1, "nbmst": "3603526528",
       "nbten": "CÔNG TY TNHH DỊCH VỤ ĂN UỐNG KIM ĐẠI PHÁT", "lookup_url": None,
       "tt_khac": {"Fkey": "873c8c9b-2f76-4ce5-878b-147f8010157d", "SearchKey": "00164181792592440"}}
KDP_TOKEN = "MTpDMjZUUEQ6MTQ0MzozNjAzNTI2NTI4"          # token thật trong mail = "1:C26TPD:1443:3603526528"


def _install_legacy(monkeypatch, resp, calls):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        assert url == "https://tracuuhddt78.hilo.com.vn/Invoice/getinvoice"
        return resp
    monkeypatch.setattr(hilo, "request", fake)
    monkeypatch.setattr(hilo, "new_session", lambda: object())


def test_legacy_token_matches_mail_link():
    assert hilo.legacy_token(KDP) == KDP_TOKEN
    assert hilo.legacy_token(dict(KDP, khhdon="1C26TPD", shdon="00001443")) == KDP_TOKEN
    assert hilo.legacy_token(dict(KDP, shdon=None)) == ""
    assert hilo.decode_legacy_token(KDP_TOKEN) == {"khmshdon": 1, "khhdon": "C26TPD", "shdon": 1443,
                                                   "nbmst": "3603526528"}
    assert hilo.decode_legacy_token("bad!!") is None
    assert hilo.decode_legacy_token("MV9DMjNUSUhfOTI3fDAzMDA2NDgyMTg=") is None   # kiểu tt78hd, khác


def test_legacy_seller_downloads_without_captcha(monkeypatch):
    calls = []
    _install_legacy(monkeypatch, _Resp(content=_pdf_with("So 1443"),
                                       headers={"Content-Type": "application/pdf"}), calls)
    assert hilo.HiloAdapter.portal_for(KDP) is None and hilo.HiloAdapter.is_legacy(KDP)
    assert hilo.HiloAdapter().fetch_pdf(KDP).startswith(b"%PDF")
    assert len(calls) == 1 and calls[0][2]["params"] == {"token": KDP_TOKEN}
    assert calls[0][2]["allow_redirects"] is False


def test_legacy_by_lookup_url_host(monkeypatch):
    inv = dict(KDP, nbmst="0314336219", khhdon="C24TVG", shdon=311,
               lookup_url="https://tracuuhddt78.hilo.com.vn/")
    calls = []
    _install_legacy(monkeypatch, _Resp(content=_pdf_with("So 311"),
                                       headers={"Content-Type": "application/pdf"}), calls)
    assert hilo.HiloAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert hilo.decode_legacy_token(calls[0][2]["params"]["token"])["nbmst"] == "0314336219"


@pytest.mark.parametrize("resp,kind", [
    (_Resp(status=302, headers={"Location": "/Invoice/Index"}), "no_pdf"),     # HĐ không có
    (_Resp(content=b"<html>x</html>", headers={"Content-Type": "text/html"}), "bad_content"),
    (_Resp(status=404), "server"),
])
def test_legacy_errors(monkeypatch, resp, kind):
    _install_legacy(monkeypatch, resp, [])
    with pytest.raises(PdfFetchError) as ei:
        hilo.HiloAdapter().fetch_pdf(KDP)
    assert ei.value.kind == kind


def test_legacy_wrong_invoice_pdf_rejected(monkeypatch):
    _install_legacy(monkeypatch, _Resp(content=_pdf_with("So 9999"),
                                       headers={"Content-Type": "application/pdf"}), [])
    with pytest.raises(PdfFetchError):
        hilo.HiloAdapter().fetch_pdf(KDP)


def test_invoice_from_link_legacy():
    a = hilo.HiloAdapter()
    inv = a.invoice_from_link(f"https://tracuuhddt78.hilo.com.vn/Invoice/getinvoice?token={KDP_TOKEN}",
                              "873c8c9b-2f76-4ce5-878b-147f8010157d", "")
    assert inv["nbmst"] == "3603526528" and inv["shdon"] == 1443 and inv["khhdon"] == "C26TPD"
    assert inv["tt_khac"] == {"Fkey": "873c8c9b-2f76-4ce5-878b-147f8010157d"}
    assert a.is_legacy(inv)
    assert a.invoice_from_link(f"https://tracuuhddt78.hilo.com.vn/Invoice/ViewFromEmail?token={KDP_TOKEN}",
                               None, "")["shdon"] == 1443
    assert a.invoice_from_link("https://tracuuhddt78.hilo.com.vn/Invoice/Download78?id=161052071",
                               "161052071", "") is None
    assert a.invoice_from_link("https://tracking.hilo.com.vn/api.php?x=1", None, "") is None
    assert pdf_providers.adapter_for_host("tracuuhddt78.hilo.com.vn").source_id == "hilo"


# --- Cổng cũ tt78hd.hilo.com.vn (token "mẫu_ký hiệu_số|MST") + link tracking ---------------

TT78HD_TOKEN = "MV9DMjNUSUhfOTI3fDAzMDA2NDgyMTg="     # token thật trong mail = "1_C23TIH_927|0300648218"


def test_decode_tt78hd_token():
    assert hilo.decode_tt78hd_token(TT78HD_TOKEN) == {"khmshdon": 1, "khhdon": "C23TIH", "shdon": 927,
                                                      "nbmst": "0300648218"}
    assert hilo.decode_tt78hd_token(KDP_TOKEN) is None            # kiểu tracuuhddt78, khác
    assert hilo.decode_tt78hd_token("bad!!") is None


def test_invoice_from_link_tt78hd_and_fetch(monkeypatch):
    a = pdf_providers.adapter_for_host("tt78hd.hilo.com.vn")
    assert a.source_id == "hilo"
    for path in ("getinvoice", "ViewFromEmail"):
        inv = a.invoice_from_link(f"https://tt78hd.hilo.com.vn/Invoice/{path}?token={TT78HD_TOKEN}",
                                  "e6d3e8d570774ba1b96541ab08b57813", "")
        assert inv["shdon"] == 927 and inv["nbmst"] == "0300648218" and inv["tt78hd_token"] == TT78HD_TOKEN
    assert a.invoice_from_link("https://tt78hd.hilo.com.vn", "e6d3e8d570774ba1b96541ab08b57813", "") is None
    calls = []

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        return _Resp(content=_pdf_with("So 927"), headers={"Content-Type": "application/pdf"})
    monkeypatch.setattr(hilo, "request", fake)
    monkeypatch.setattr(hilo, "new_session", lambda: object())
    assert a.fetch_pdf(dict(inv)).startswith(b"%PDF")
    assert calls[0][1] == "https://tt78hd.hilo.com.vn/Invoice/getinvoice"
    assert calls[0][2]["params"] == {"token": TT78HD_TOKEN} and calls[0][2]["allow_redirects"] is False
    with pytest.raises(PdfFetchError):                                # PDF phải đúng số HĐ
        a.fetch_pdf(dict(inv, shdon=928))


def test_tt78hd_not_found_is_no_pdf(monkeypatch):
    monkeypatch.setattr(hilo, "request", lambda s, m, u, **kw: _Resp(status=302, headers={"Location": "/Invoice/Index"}))
    monkeypatch.setattr(hilo, "new_session", lambda: object())
    inv = hilo.HiloAdapter().invoice_from_link(f"https://tt78hd.hilo.com.vn/Invoice/getinvoice?token={TT78HD_TOKEN}", None, "")
    with pytest.raises(PdfFetchError) as ei:
        hilo.HiloAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_pdf"


def test_tracking_links_are_claimed_but_ignored():
    a = pdf_providers.adapter_for_host("tracking.hilo.com.vn")
    assert a.source_id == "hilo"
    url = ("https://tracking.hilo.com.vn/api.php?platform=email&position=footer&ad_idx=0&type=$Type"
           "&taxcode=3603526528&custaxcode=1201496252&req_type=click")
    assert a.invoice_from_link(url, "873c8c9b-2f76-4ce5-878b-147f8010157d", "") is None
