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


PDF_LINK = "https://han-tt78.vnpt-invoice.com.vn/Email/PdfDownload?token=w3zk%2bKih%3d"   # token giả


def test_invoice_from_link_email_pdf_download(monkeypatch):
    a = vnpt.VnptAdapter()
    inv = a.invoice_from_link(PDF_LINK, None, "ACV. Thông báo phát hành Hóa Đơn Điện Tử")
    assert inv == {"email_pdf_token": "w3zk+Kih=", "lookup_url": "https://han-tt78.vnpt-invoice.com.vn"}
    assert a.invoice_from_link("https://han-tt78.vnpt-invoice.com.vn/Email/PdfDownload", None, "") is None
    calls = []
    _router(monkeypatch, [("GET", "/Email/PdfDownload", _Resp(content=_pdf_with("So 123")))], calls)
    assert common.is_pdf(a.fetch_pdf(dict(inv)))
    assert calls[0][2]["params"] == {"token": "w3zk+Kih="}           # không giải captcha, không tra Fkey
    with pytest.raises(PdfFetchError) as ei:                          # không có Fkey → XML bỏ qua, không gọi mạng
        a.fetch_xml(dict(inv))
    assert ei.value.kind == "no_code" and len(calls) == 1


def test_invoice_from_link_noibai_and_underscore_host():
    """ACV Nội Bài: link trang chủ invoice-noibai.vietnamairport.vn + "Mã tra cứu nhanh" → cổng han-tt78."""
    a = vnpt.VnptAdapter()
    assert pdf_providers.adapter_for_host("invoice-noibai.vietnamairport.vn") is pdf_providers.get_adapter("vnpt")
    inv = a.invoice_from_link("https://invoice-noibai.vietnamairport.vn", None,
                              "ACV. Thông báo phát hành Hóa Đơn Điện Tử\nX3100004261402EP")
    assert inv == {"ma_tra_cuu": "X3100004261402EP", "tt_khac": {"Fkey": "X3100004261402EP"},
                   "lookup_url": "https://han-tt78.vnpt-invoice.com.vn"}
    inv = a.invoice_from_link("https://dimtutac_dongdu-tt78.vnpt-invoice.com.vn/Email/PdfDownload?token=SI4S%3d",
                              None, "")
    assert inv == {"email_pdf_token": "SI4S=", "lookup_url": "https://dimtutac_dongdu-tt78.vnpt-invoice.com.vn"}


def test_email_pdf_download_not_pdf(monkeypatch):
    _router(monkeypatch, [("GET", "/Email/PdfDownload", _Resp(content=b"<html>het han</html>"))], [])
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf({"email_pdf_token": "x", "lookup_url": "https://han-tt78.vnpt-invoice.com.vn"})
    assert ei.value.kind == "no_pdf"


@pytest.mark.parametrize("url, text", [
    ("https://han-tt78.vnpt-invoice.com.vn/invoice/getinvoice?token=abc", "ACV"),          # link khác cùng mail
    ("https://vttphcm-tt78.vnpt-invoice.com.vn/", "Để tải hóa đơn điện tử vui lòng truy cập: ..."),  # không mã
    ("https://vttphcm-tt78.vnpt-invoice.com.vn/", "FW: Tổng hợp thông báo cước"),
    ("https://portal-hst.vnpt-invoice.com.vn/", MAIL),                     # cổng VNPT khác: chưa nhận link
    ("https://tracuuhoadon.vnpt-invoice.com.vn/", MAIL),
])
def test_invoice_from_link_unusable(url, text):
    assert vnpt.VnptAdapter().invoice_from_link(url, None, text) is None


# --- Người bán có cổng -tt78 riêng, mã tra cứu là trường khác của XML -----------------

GOLF = {"shdon": 24387, "khhdon": "C26TLT", "khmshdon": 1, "nbmst": "3600718503",
        "nbten": "CÔNG TY CỔ PHẦN ĐẦU TƯ VÀ KINH DOANH GOLF LONG THÀNH",
        "tt_khac": {"Extra": "R20260505-0079"}, "ma_cqt": "00B3AD55267F584C659E517C9D484615E4"}
SW2 = {"shdon": 888, "khhdon": "C24TAA", "khmshdon": 1, "nbmst": "0313894637-002",
       "nbten": "CÔNG TY CỔ PHẦN THỰC PHẨM & ĐỒ UỐNG SW - CHI NHÁNH 2", "tt_khac": {},
       "ma_cqt": "008E9A81FF5E014211AADC1EA23F8AE172"}


@pytest.mark.parametrize("inv,base,key", [
    (GOLF, "https://golflongthanh-tt78.vnpt-invoice.com.vn", "R20260505-0079"),
    (dict(GOLF, tt_khac={}), "https://golflongthanh-tt78.vnpt-invoice.com.vn",
     "00B3AD55267F584C659E517C9D484615E4"),                              # không có Extra → MCCQT
    (SW2, "https://swfoodsbeveragescn2-tt78.vnpt-invoice.com.vn", "008E9A81FF5E014211AADC1EA23F8AE172"),
    ({"nbmst": "0304836029", "tt_khac": {"Extra3": "0"}, "ma_cqt": "00725E15"},
     "https://navigos-tt78.vnpt-invoice.com.vn", "00725E15"),
])
def test_seller_portal_and_key(inv, base, key):
    assert vnpt.VnptAdapter.portal_for(inv) == base
    assert vnpt.VnptAdapter.fkey(inv) == key


def test_xml_fkey_wins_over_seller_key():
    inv = dict(SW2, tt_khac={"Fkey": "REALFKEY"})
    assert vnpt.VnptAdapter.fkey(inv) == "REALFKEY"


def test_seller_without_keys_does_not_use_ma_cqt(tmp_path):
    inv = {"nbmst": "0300954529", "tt_khac": {}, "ma_cqt": "00AB", "xml_path": str(tmp_path / "x.xml")}
    assert vnpt.VnptAdapter.fkey(inv) == ""


def test_branch_matches_parent_mst_but_exact_wins():
    petro_cn = {"nbmst": "0300555450-001",
                "nbten": "CHI NHÁNH XĂNG DẦU SÀI GÒN - CÔNG TY XĂNG DẦU KHU VỰC II TNHH MỘT THÀNH VIÊN"}
    assert vnpt.VnptAdapter.portal_for(petro_cn) == "https://hoadon.petrolimex.com.vn"
    # SW chi nhánh 1 (BKAV) không được khớp sang cổng của chi nhánh 2; MST mẹ cũng không
    assert vnpt.VnptAdapter.portal_for({"nbmst": "0313894637-001"}) is None
    assert vnpt.VnptAdapter.portal_for({"nbmst": "0313894637"}) is None


def test_golf_downloads_with_extra_code(monkeypatch, captcha_ok):
    calls = []
    _router(monkeypatch, [(m, f, r) for m, f, r in _routes(_found(), pdf=_pdf_with("So 24387"))], calls)
    assert vnpt.VnptAdapter().fetch_pdf(GOLF).startswith(b"%PDF")
    search = [kw["data"] for m, u, kw in calls if u.endswith("/HomeNoLogin/SearchByFkey")]
    assert search[0]["strFkey"] == "R20260505-0079"
    assert all(u.startswith("https://golflongthanh-tt78.") for m, u, kw in calls)


def test_low_confidence_captcha_fetches_new_one(monkeypatch):
    answers = [PdfFetchError("Model không chắc captcha", "captcha"), "1234"]

    def solve(img, group, min_conf):
        assert group == "g_80x30"
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(vnpt, "solve_captcha", solve)
    calls = []
    _router(monkeypatch, _routes(_found()), calls)
    assert vnpt.VnptAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert sum(u.endswith("/Captcha/Show") for m, u, kw in calls) == 2
    assert sum(u.endswith("/HomeNoLogin/SearchByFkey") for m, u, kw in calls) == 1   # không gửi bừa


# --- Link mail Petrolimex (cổng riêng dạng SearchInvoicebycode) -----------------------

PETRO = "https://hoadon.petrolimex.com.vn"
PETRO_SUBJECT = "Thông báo phát hành CHI NHÁNH XĂNG DẦU SÀI GÒN - CÔNG TY XĂNG DẦU KHU VỰC II TNHH MỘT THÀNH VIÊN"


def test_petrolimex_link_host_and_code():
    assert pdf_providers.adapter_for_host("hoadon.petrolimex.com.vn").source_id == "vnpt"
    a = vnpt.VnptAdapter()
    inv = a.invoice_from_link(PETRO, "AB12CDEFG*", f"{PETRO_SUBJECT}\nAB12CDEFG*")   # mã giả, giữ dấu *
    assert inv == {"ma_tra_cuu": "AB12CDEFG*", "tt_khac": {"Fkey": "AB12CDEFG*"}, "lookup_url": PETRO}
    assert "nbmst" not in inv                      # mail của cả công ty mẹ lẫn chi nhánh -001: không đoán
    assert a.portal_for(inv) == PETRO and a.fkey(inv) == "AB12CDEFG*"
    # mã chỉ có trong mail_text (dòng sau tiêu đề)
    assert a.invoice_from_link(f"{PETRO}/", None, f"{PETRO_SUBJECT}\nAB12CDEFG*")["ma_tra_cuu"] == "AB12CDEFG*"
    assert a.invoice_from_link(PETRO, None, PETRO_SUBJECT) is None                     # không có mã


def test_petrolimex_link_fetch_uses_search_form(monkeypatch):
    a = vnpt.VnptAdapter()
    seen = []
    monkeypatch.setattr(a, "_search", lambda base, fkey: seen.append((base, fkey)) or "CHK")
    monkeypatch.setattr(a, "_render", lambda base, code, inv: seen.append((base, code)) or b"%PDF")
    inv = a.invoice_from_link(PETRO, "AB12CDEFG*", PETRO_SUBJECT)
    assert a.fetch_pdf(inv) == b"%PDF"
    assert seen == [(PETRO, "AB12CDEFG*"), (PETRO, "CHK")]


def test_portal_by_lookup_url_only_known_hosts():
    assert vnpt.VnptAdapter.portal_for({"lookup_url": "https://hoadon.petrolimex.com.vn/"}) == PETRO
    assert vnpt.VnptAdapter.portal_for({"lookup_url": "https://hoadon.petrolimex.com.vn.evil.vn/"}) is None
    assert vnpt.VnptAdapter().invoice_from_link("https://evil.vn/", "AB12CDEFG*", "") is None
