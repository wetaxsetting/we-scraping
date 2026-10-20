"""Adapter VNPT — cổng <alias>-tt78.vnpt-invoice.com.vn (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 10/2026 trên cổng VNPT TP.HCM (vttphcm-tt78, HĐ K26THA-6471438):
captcha g_80x30 đọc đúng 13/13; Fkey không có HĐ → server treo (hết giờ). Mã trong test là mã GIẢ.

Run::

    pytest tests/test_pdf_vnpt_tt78.py -q
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
from pdf_providers import common           # noqa: E402
from pdf_providers import vnpt             # noqa: E402

BASE = "https://vttphcm-tt78.vnpt-invoice.com.vn"
FKEY = "HCM12345678202601"                 # mã giả
INV = {"shdon": 7654321, "khhdon": "K26THA", "khmshdon": 1, "nbmst": "0300954529",
       "nbten": "VIỄN THÔNG THÀNH PHỐ HỒ CHÍ MINH", "tt_khac": {"Fkey": FKEY}}
_HOME = ('<form class="search" id="Searchform" method="post" action="/HomeNoLogin/SearchByFkey">'
         '<input name="__RequestVerificationToken" type="hidden" value="TOK78" />'
         '<input type="hidden" name="isHomepage" id="isHomepage" value="true"/></form>'
         '<form action="/Account/LogOn" id="IdLogOn"><input name="__RequestVerificationToken" '
         'type="hidden" value="LOGIN" /></form>')
_XML = (b"<?xml-stylesheet type='text/xsl' href='x' ?><HDon><DLHDon Id=\"DuLieuKy\"><TTChung>"
        b"<KHHDon>K26THA</KHHDon><SHDon>7654321</SHDon></TTChung></DLHDon><Fkey>" + FKEY.encode()
        + b"</Fkey></HDon>")


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):                      # đủ dài để vượt ngưỡng is_pdf
        page.insert_text((40, 40 + i * 18), f"{text} dong {i}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


class _Resp:
    def __init__(self, status=200, content=b"", text=""):
        self.status_code, self.content, self.text = status, content, text


def _found():
    return _Resp(text="<a onclick=\"ajxCall4Portal('CC/abc+1=','Tải hóa đơn Zip',null,'x');\">Xem</a>"
                      "<a href=\"/HomeNoLogin/downloadPDF?checkCode=CC/abc+1=&amp;fkey=x\">pdf</a>")


def _error(msg):
    return _Resp(text=f'<div class="errorbox"> <img src="/Content/Images/cross.gif" alt="Close" /> {msg}<br /></div>')


def _routes(search, pdf=None, xml=_XML):
    return [
        ("GET", "/Captcha/Show", _Resp(content=b"png")),
        ("POST", "/HomeNoLogin/SearchByFkey", search),
        ("GET", "/HomeNoLogin/downloadPDF", _Resp(content=pdf if pdf is not None else _pdf_with("So 7654321"))),
        ("GET", "/Invoice/Download", _Resp(content=xml)),
        ("GET", "tt78.vnpt-invoice.com.vn/", _Resp(text=_HOME)),
    ]


def _router(monkeypatch, routes, calls):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        for m, frag, resp in routes:
            if m == method and frag in url:
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(vnpt, "request", fake)
    monkeypatch.setattr(vnpt, "new_session", lambda: object())


@pytest.fixture
def captcha_ok(monkeypatch):
    monkeypatch.setattr(vnpt, "solve_captcha", lambda img, group, min_conf: "1234")


# --- Định tuyến ---------------------------------------------------------------

def test_portal_for_seller_and_tt78_lookup_url():
    assert vnpt.VnptAdapter.portal_for(INV) == BASE
    by_link = {"nbmst": None, "lookup_url": "https://abc-tt78.vnpt-invoice.com.vn/"}
    assert vnpt.VnptAdapter.portal_for(by_link) == "https://abc-tt78.vnpt-invoice.com.vn"
    assert vnpt.VnptAdapter.portal_for({"nbmst": "0312345678",
                                        "lookup_url": "https://portal-hst.vnpt-invoice.com.vn"}) is None
    assert vnpt.VnptAdapter.portal_for({"nbmst": "0312345678",
                                        "lookup_url": "https://x-tt78.vnpt-invoice.com.vn.evil.vn"}) is None


def test_adapter_for_host():
    assert pdf_providers.adapter_for_host("vttphcm-tt78.vnpt-invoice.com.vn").source_id == "vnpt"
    assert pdf_providers.adapter_for_host("evil-vnpt-invoice.com.vn") is None


# --- Tải PDF -----------------------------------------------------------------

def test_tt78_ok_server_pdf(monkeypatch, captcha_ok):
    calls = []
    _router(monkeypatch, _routes(_found()), calls)
    assert vnpt.VnptAdapter().fetch_pdf(INV).startswith(b"%PDF")
    search = next(kw for m, u, kw in calls if u.endswith("/HomeNoLogin/SearchByFkey"))
    assert search["data"] == {"__RequestVerificationToken": "TOK78", "isHomepage": "true",
                              "strFkey": FKEY, "captch": "1234"}
    assert search["timeout"] == vnpt.TT78_SEARCH_TIMEOUT
    dl = next(kw for m, u, kw in calls if u.endswith("/HomeNoLogin/downloadPDF"))
    assert dl["params"] == {"checkCode": "CC/abc+1=", "fkey": FKEY}
    assert not any("SearchInvoicebycode" in u for m, u, kw in calls)          # không đi luồng Petrolimex


def test_tt78_retries_wrong_captcha_then_ok(monkeypatch, captcha_ok):
    pages = iter([_error("Mã xác thực không chính xác"), _found()])
    _router(monkeypatch, _routes(lambda url, kw: next(pages)), [])
    assert vnpt.VnptAdapter().fetch_pdf(INV).startswith(b"%PDF")


def test_tt78_gives_up_on_captcha(monkeypatch, captcha_ok):
    calls = []
    _router(monkeypatch, _routes(_error("Mã xác thực không chính xác")), calls)
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"
    assert sum(u.endswith("/SearchByFkey") for m, u, kw in calls) == vnpt.CAPTCHA_TRIES


def test_tt78_refuses_non_4_digit_answer(monkeypatch):
    calls = []
    _router(monkeypatch, _routes(_found()), calls)
    monkeypatch.setattr(vnpt, "solve_captcha", lambda img, group, min_conf: "12a4")
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"
    assert not any(u.endswith("/SearchByFkey") for m, u, kw in calls)         # không gửi bừa


def test_tt78_search_timeout_means_not_found(monkeypatch, captcha_ok):
    def hang(url, kw):
        raise PdfFetchError(f"Timeout {url}: read timed out", "network")
    _router(monkeypatch, _routes(hang), [])
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_tt78_connection_error_stays_network(monkeypatch, captcha_ok):
    def down(url, kw):
        raise PdfFetchError(f"Lỗi kết nối {url}: reset", "network")
    _router(monkeypatch, _routes(down), [])
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(INV)
    assert ei.value.kind == "network"


@pytest.mark.parametrize("pdf", [b"<html>" + b"x" * 2000, _pdf_with("So 1111111")])
def test_tt78_bad_pdf(monkeypatch, captcha_ok, pdf):
    _router(monkeypatch, _routes(_found(), pdf=pdf), [])
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


def test_tt78_no_checkcode_is_bad_content(monkeypatch, captcha_ok):
    _router(monkeypatch, _routes(_Resp(text="<html>trang lạ</html>")), [])
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


# --- Fkey ở <HDon><Fkey> (xml_parser chưa đọc) → đọc lại file theo xml_path -----

def test_fkey_from_saved_xml_relative_to_output_dir(monkeypatch, tmp_path, captcha_ok):
    (tmp_path / "xml").mkdir()
    (tmp_path / "xml" / "a.xml").write_bytes(_XML)
    monkeypatch.setenv("INVOICE_OUTPUT_DIR", str(tmp_path))
    inv = {**INV, "tt_khac": {"Extra": ";;;"}, "ma_tra_cuu": None, "xml_path": "xml/a.xml"}
    assert vnpt.VnptAdapter.fkey(inv) == FKEY
    calls = []
    _router(monkeypatch, _routes(_found()), calls)
    assert vnpt.VnptAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_no_fkey_anywhere_is_no_code(tmp_path):
    inv = {**INV, "tt_khac": {}, "ma_tra_cuu": "", "xml_path": str(tmp_path / "khong-co.xml")}
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


# --- XML -----------------------------------------------------------------------

def test_fetch_xml_then_pdf_solves_one_captcha(monkeypatch, captcha_ok):
    calls = []
    _router(monkeypatch, _routes(_found()), calls)
    a = vnpt.VnptAdapter()
    assert a.fetch_xml(INV) == _XML
    assert a.fetch_pdf(INV).startswith(b"%PDF")
    assert sum(u.endswith("/SearchByFkey") for m, u, kw in calls) == 1        # checkCode dùng lại
    dl = next(kw for m, u, kw in calls if u.endswith("/Invoice/Download"))
    assert dl["params"] == {"checkCode": "CC/abc+1=", "fkey": FKEY, "nameCus": ""}


@pytest.mark.parametrize("xml", [b"<html>loi</html>", _XML.replace(b"7654321", b"1111111")])
def test_fetch_xml_rejects_wrong_content(monkeypatch, captcha_ok, xml):
    _router(monkeypatch, _routes(_found(), xml=xml), [])
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_xml(INV)
    assert ei.value.kind == "bad_content"


def test_fetch_xml_only_on_tt78():
    petro = {"shdon": 1, "nbmst": "0300555450", "tt_khac": {"Fkey": "ABC123*"}}
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_xml(petro)
    assert ei.value.kind == "no_portal"


# --- Link trong mail -----------------------------------------------------------

MAIL = ("Kính gửi Quý khách hàng, ... Hóa đơn của khách hàng có số 07654321 thuộc mẫu 1/023 và serial "
        "K26THA Quý khách vui lòng truy cập trang Web: https://vttphcm-tt78.vnpt-invoice.com.vn để tra cứu "
        f"hóa đơn. Mã tra cứu hóa đơn: {FKEY} Trân trọng")


def test_invoice_from_link_reads_code_and_number_from_mail():
    inv = vnpt.VnptAdapter().invoice_from_link("https://vttphcm-tt78.vnpt-invoice.com.vn", None, MAIL)
    assert inv == {"ma_tra_cuu": FKEY, "tt_khac": {"Fkey": FKEY}, "lookup_url": BASE,
                   "nbmst": "0300954529", "shdon": 7654321, "khmshdon": 1, "khhdon": "K26THA"}
    assert vnpt.VnptAdapter.portal_for(inv) == BASE


def test_invoice_from_link_code_from_process_links_lines():
    a = vnpt.VnptAdapter()
    # process_links: mail_text = tiêu đề + mỗi mã 1 dòng; mã cũng có thể đến qua tham số code
    inv = a.invoice_from_link("https://abc-tt78.vnpt-invoice.com.vn/", None, f"FW: Thông báo phát hành HDDT\n{FKEY}")
    assert inv["ma_tra_cuu"] == FKEY and inv["lookup_url"] == "https://abc-tt78.vnpt-invoice.com.vn"
    assert "nbmst" not in inv                                                  # cổng chưa gắn người bán
    assert a.invoice_from_link("https://abc-tt78.vnpt-invoice.com.vn/", FKEY, "")["ma_tra_cuu"] == FKEY


@pytest.mark.parametrize("url, text", [
    ("https://vttphcm-tt78.vnpt-invoice.com.vn/", "Để tải hóa đơn điện tử vui lòng truy cập: ..."),  # không mã
    ("https://vttphcm-tt78.vnpt-invoice.com.vn/", "FW: Tổng hợp thông báo cước"),
    ("https://portal-hst.vnpt-invoice.com.vn/", MAIL),                     # cổng VNPT khác: chưa nhận link
    ("https://tracuuhoadon.vnpt-invoice.com.vn/", MAIL),
])
def test_invoice_from_link_unusable(url, text):
    assert vnpt.VnptAdapter().invoice_from_link(url, None, text) is None
