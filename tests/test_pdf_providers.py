"""Adapter tải PDF: tiện ích dùng chung + đăng ký adapter (không cần mạng).

Adapter sai không làm job đổ — nó lưu nhầm trang lỗi HTML thành "PDF", hoặc gửi
captcha đoán bừa và đốt lượt thử. Nên kiểm các điểm chặn đó.

Run::

    pytest tests/test_pdf_providers.py -q
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402


def test_is_pdf():
    assert common.is_pdf(b"%PDF-1.7\n" + b"x" * 2000)
    assert not common.is_pdf(b"<html>loi</html>" + b"x" * 2000)
    assert not common.is_pdf(b"%PDF-1.7")            # quá ngắn: thường là trang lỗi
    assert not common.is_pdf(None)


class _Sess:
    def __init__(self, result):
        self.result = result

    def request(self, method, url, **kw):
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(status_code=self.result)


@pytest.mark.parametrize("result, kind", [
    (requests.Timeout("t"), "network"),
    (requests.ConnectionError("c"), "network"),
    (429, "rate_limit"),
    (503, "server"),
])
def test_request_classifies_errors(result, kind):
    with pytest.raises(PdfFetchError) as ei:
        common.request(_Sess(result), "GET", "https://x")
    assert ei.value.kind == kind


def test_request_ok_passthrough():
    assert common.request(_Sess(200), "GET", "https://x").status_code == 200


def _png(mode="RGBA"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, (150, 34), (0, 0, 0, 0) if mode == "RGBA" else (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


def _fake_predictor(text, conf, error=""):
    class P:
        def predict_path(self, path):
            assert Path(path).exists()
            return SimpleNamespace(text=text, confidence=conf, error=error)
    return lambda group_id: P()


def test_solve_captcha_ok(monkeypatch):
    monkeypatch.setattr(common, "_predictor", _fake_predictor("123456", 0.97))
    assert common.solve_captcha(_png(), "g_150x34") == "123456"


@pytest.mark.parametrize("text, conf, err", [("12", 0.2, ""), ("", 0.9, ""), ("1", 0.9, "boom")])
def test_solve_captcha_refuses_unsure(monkeypatch, text, conf, err):
    monkeypatch.setattr(common, "_predictor", _fake_predictor(text, conf, err))
    with pytest.raises(PdfFetchError) as ei:
        common.solve_captcha(_png("RGB"), "g_150x34")
    assert ei.value.kind == "captcha"


def test_solve_captcha_rejects_unknown_group():
    with pytest.raises(Exception):
        common._predictor("g_khong_ton_tai")


# ---------------------------------------------------------------------------
# Adapter cụ thể, HTTP giả lập (luồng thật đã kiểm trên HĐ thật 10/2026)
# ---------------------------------------------------------------------------

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
    def __init__(self, status=200, content=b"", text="", js=None, headers=None):
        self.status_code, self.content, self.text = status, content, text
        self._js, self.headers = js, headers or {}

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


def _router(module, monkeypatch, routes, calls):
    """Thay request() của adapter: routes = list[(method, url_contains, resp|callable)]."""
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        for m, frag, resp in routes:
            if m == method and frag in url:
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(module, "request", fake)
    monkeypatch.setattr(module, "new_session", lambda: object())


INV = {"shdon": 4538, "khhdon": "C26TPS", "khmshdon": 1, "nbmst": "0104947250-001",
       "tt_khac": {"Fkey": "X2KI57TA8", "TransactionID": "L5FECDJ0X75Z",
                   "PortalLink": "http://0104947250001hd.easyinvoice.com.vn"}}


# --- MISA -------------------------------------------------------------------

from pdf_providers import misa  # noqa: E402


def test_misa_ok_sends_browser_accept(monkeypatch):
    calls = []
    _router(misa, monkeypatch, [("GET", "DownloadHandler", _Resp(content=_pdf_with("HD 4538")))], calls)
    assert misa.MisaAdapter().fetch_pdf(INV).startswith(b"%PDF")
    _, _, kw = calls[0]
    assert kw["params"] == {"Type": "pdf", "Code": "L5FECDJ0X75Z"}
    assert "text/html" in kw["headers"]["Accept"]          # thiếu → server trả body rỗng


@pytest.mark.parametrize("resp, kind", [
    (_Resp(content=b""), "no_pdf"),                         # mã sai / chưa cấp mã
    (_Resp(content=b"<html>" + b"x" * 2000), "bad_content"),
    (_Resp(status=404), "other"),
])
def test_misa_errors(monkeypatch, resp, kind):
    _router(misa, monkeypatch, [("GET", "DownloadHandler", resp)], [])
    with pytest.raises(PdfFetchError) as ei:
        misa.MisaAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_misa_rejects_pdf_of_another_invoice(monkeypatch):
    _router(misa, monkeypatch, [("GET", "DownloadHandler", _Resp(content=_pdf_with("HD 9999")))], [])
    with pytest.raises(PdfFetchError) as ei:
        misa.MisaAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


def test_misa_without_code():
    with pytest.raises(PdfFetchError) as ei:
        misa.MisaAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


# --- EasyInvoice ------------------------------------------------------------

from pdf_providers import easyinvoice  # noqa: E402


def _search_page(msg="", ok=True):
    inv = ('{&quot;str&quot;:&quot;&lt;div&gt;HD&lt;/div&gt;&quot;,&quot;pattern&quot;:&quot;1C26TPS&quot;}'
           if ok else "")
    return _Resp(text=f'<input id="msg" name="msg" type="hidden" value="{msg}" />'
                      f'<input id="InvData" name="InvData" type="hidden" value="{inv}" />'
                      + ("<script>showInv(data.str, 1, 'TOKEN123');</script>" if ok else ""))


def _zip_pdf(pdf: bytes) -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("4538_1C26TPS.pdf", pdf)
    return buf.getvalue()


def _easy_routes(search):
    return [
        ("GET", "/Search/Index", _Resp()),
        ("GET", "/Captcha/Show", _Resp(content=b"png")),
        ("POST", "/Search/Search", search),
        ("POST", "DownloadPdfAndFileAttachFromAvailableHtml",
         _Resp(js={"fileGuid": "g1", "fileName": "HOADON.zip"})),
        ("GET", "/Invoice/Download", _Resp(content=_zip_pdf(_pdf_with("HD 4538")))),
    ]


def test_easyinvoice_ok_via_generic_portal(monkeypatch):
    calls = []
    _router(easyinvoice, monkeypatch, _easy_routes(_search_page()), calls)
    monkeypatch.setattr(easyinvoice, "solve_captcha", lambda img, group: "1234")
    assert easyinvoice.EasyInvoiceAdapter().fetch_pdf(INV).startswith(b"%PDF")
    search = next(kw for m, u, kw in calls if u.endswith("/Search/Search"))
    assert search["data"] == {"typeSearch": "", "FKey": "X2KI57TA8", "Capcha": "1234",
                              "TaxCode": "0104947250-001"}         # MST giữ dấu '-'
    gen = next(kw for m, u, kw in calls if "AvailableHtml" in u)
    assert gen["data"]["token"] == "TOKEN123"
    assert base64_decode(gen["data"]["html"]) == "<div>HD</div>"   # gửi đúng HTML hóa đơn


def base64_decode(s):
    import base64
    return base64.b64decode(s).decode("utf-8")


def test_easyinvoice_retries_wrong_captcha_then_succeeds(monkeypatch):
    pages = iter([_search_page("Nhập đúng mã capcha", ok=False),
                  _search_page("Nhập đúng mã capcha", ok=False), _search_page()])
    routes = _easy_routes(lambda url, kw: next(pages))
    _router(easyinvoice, monkeypatch, routes, [])
    monkeypatch.setattr(easyinvoice, "solve_captcha", lambda img, group: "0000")
    assert easyinvoice.EasyInvoiceAdapter().fetch_pdf(INV).startswith(b"%PDF")


def test_easyinvoice_gives_up_on_captcha(monkeypatch):
    _router(easyinvoice, monkeypatch,
            _easy_routes(_search_page("Nhập đúng mã capcha", ok=False)), [])
    monkeypatch.setattr(easyinvoice, "solve_captcha", lambda img, group: "0000")
    with pytest.raises(PdfFetchError) as ei:
        easyinvoice.EasyInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"


def test_easyinvoice_falls_back_to_seller_portal(monkeypatch):
    calls = []

    def search(url, kw):
        if "tracuu.easyinvoice.vn" in url:
            return _search_page("MST công ty không tồn tại", ok=False)
        return _search_page()
    _router(easyinvoice, monkeypatch, _easy_routes(search), calls)
    monkeypatch.setattr(easyinvoice, "solve_captcha", lambda img, group: "1234")
    assert easyinvoice.EasyInvoiceAdapter().fetch_pdf(INV).startswith(b"%PDF")
    seller_search = [kw for m, u, kw in calls
                     if u == "http://0104947250001hd.easyinvoice.com.vn/Search/Search"]
    assert seller_search and "TaxCode" not in seller_search[0]["data"]   # cổng riêng chỉ cần Fkey


def test_easyinvoice_not_found_everywhere(monkeypatch):
    _router(easyinvoice, monkeypatch,
            _easy_routes(_search_page("Không tồn tại hóa đơn", ok=False)), [])
    monkeypatch.setattr(easyinvoice, "solve_captcha", lambda img, group: "1234")
    with pytest.raises(PdfFetchError) as ei:
        easyinvoice.EasyInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_easyinvoice_portal_derived_from_seller_mst():
    portals = easyinvoice.EasyInvoiceAdapter.portals({"nbmst": "0109045760", "tt_khac": {}})
    assert portals == [("https://tracuu.easyinvoice.vn", True),
                       ("http://0109045760hd.easyinvoice.com.vn", False)]


def test_registry_maps_mst_tcgp_to_adapters():
    assert pdf_providers.adapter_for_mst_tcgp("0101243150").source_id == "misa_meinvoice"
    assert pdf_providers.adapter_for_mst_tcgp("0105987432").source_id == "easyinvoice"
    assert pdf_providers.adapter_for_mst_tcgp("0100684378").source_id == "vnpt"
    assert pdf_providers.adapter_for_mst_tcgp("0000000000") is None


# --- VNPT -------------------------------------------------------------------

from pdf_providers import vnpt  # noqa: E402

VNPT_INV = {"shdon": 2296483, "khhdon": "K26TAN", "nbmst": "0300555450",
            "nbten": "CÔNG TY TNHH MTV PETROLIMEX SÀI GÒN", "tt_khac": {"Fkey": "7YZCACMAG*"}}
_FORM = ('<form id="SearchformByfkey"><input name="__RequestVerificationToken" '
         'type="hidden" value="TOK" /></form>')


def _vnpt_result(msg=""):
    if msg:
        return _Resp(text=f'<div class="errorbox"><img src="x.png"/> {msg}</div>')
    return _Resp(text="<a onclick=\"ajxCall4Portal('CHECK+CODE/1')\">Xem</a>")


def _vnpt_routes(search):
    return [
        ("GET", "/Captcha/Show", _Resp(content=b"png")),
        ("POST", "/SearchInvoicebycode/Index", search),
        ("POST", "/SearchInvoicebycode/ajxPreview/", _Resp(js={"str": "<div>HD 2296483</div>"})),
        ("GET", "petrolimex.com.vn/", _Resp(text=_FORM)),
    ]


def test_vnpt_portal_mapping():
    assert vnpt.VnptAdapter.portal_for(VNPT_INV) == "https://hoadon.petrolimex.com.vn"
    other_petrolimex = {"nbmst": "0100107370", "nbten": "Công ty Xăng dầu Petrolimex Hà Nội"}
    assert vnpt.VnptAdapter.portal_for(other_petrolimex) == "https://hoadon.petrolimex.com.vn"
    assert vnpt.VnptAdapter.portal_for({"nbmst": "0312345678", "nbten": "Cty X"}) is None


HST_NOT_FOUND = _Resp(js={"title": "404", "status": 200, "err_code": 1,
                          "message": "Không tìm thấy hóa đơn"})
HST_FOUND = _Resp(js={"err_code": "0", "content": {"id": 777, "fKey": "FK-HST"}})
OTHER_SELLER = {**VNPT_INV, "nbmst": "0312345678", "nbten": "Cty X"}


def test_vnpt_unknown_seller_not_on_hst_parks(monkeypatch):
    calls = []
    _router(vnpt, monkeypatch, [("GET", "/invoice/api/invoice/portal/key", HST_NOT_FOUND)], calls)
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(OTHER_SELLER)
    assert ei.value.kind == "no_portal"
    assert len(calls) == 1 and calls[0][2]["params"] == {"key": "7YZCACMAG*"}   # 1 GET, không captcha


def test_vnpt_hst_server_pdf(monkeypatch):
    import base64
    calls = []
    pdf = _pdf_with("HD 2296483")
    _router(vnpt, monkeypatch, [
        ("GET", "/portal/key", HST_FOUND),
        ("POST", "/portal/download-pdf", _Resp(js={"content": {"data": base64.b64encode(pdf).decode()}})),
    ], calls)
    assert vnpt.VnptAdapter().fetch_pdf(OTHER_SELLER) == pdf
    dl = next(kw for m, u, kw in calls if u.endswith("/portal/download-pdf"))
    assert dl["json"] == {"ids": [777], "fkey": "FK-HST"} and dl["headers"]["app"] == "portal"


def test_vnpt_hst_falls_back_to_server_html_to_pdf(monkeypatch):
    pdf = _pdf_with("HD 2296483")
    _router(vnpt, monkeypatch, [
        ("GET", "/portal/key", HST_FOUND),
        ("POST", "/portal/download-pdf", _Resp(js={"content": {}})),
        ("POST", "/portal/download-htmlv2", _Resp(js={"content": {"view": "<div>HD</div>"}})),
        ("POST", "/portal/html-to-pdf", _Resp(content=pdf)),
    ], [])
    assert vnpt.VnptAdapter().fetch_pdf(OTHER_SELLER) == pdf


def test_vnpt_hst_mtt_uses_pos_api(monkeypatch):
    from decimal import Decimal
    calls = []
    _router(vnpt, monkeypatch, [("POST", "/pos/api/posinvoice/portal/find-by-mccqt", HST_NOT_FOUND),
                                ("GET", "/pos/api/posinvoice/portal/key", HST_NOT_FOUND)], calls)
    mtt = {**OTHER_SELLER, "sub_type": "mua-mtt"}
    with pytest.raises(PdfFetchError):
        vnpt.VnptAdapter().fetch_pdf({**mtt, "tt_khac": {"Fkey": "abc*"}})
    assert calls[-1][2]["params"] == {"key": "ABC*"}                      # MTT: mã viết HOA
    with pytest.raises(PdfFetchError):                                    # không Fkey → tra MCCQT
        vnpt.VnptAdapter().fetch_pdf({**mtt, "tt_khac": {}, "ma_tra_cuu": "",
                                      "ma_cqt": "M1-26-A1B2C-00001234567", "tong_tien": Decimal("25000")})
    assert calls[-1][2]["json"] == {"mccqt": "M1-26-A1B2C-00001234567", "totalPay": 25000}


def test_vnpt_ok_keeps_trailing_star_and_renders(monkeypatch):
    calls = []
    _router(vnpt, monkeypatch, _vnpt_routes(_vnpt_result()), calls)
    monkeypatch.setattr(vnpt, "solve_captcha", lambda img, group, min_conf: "1234")
    import pdf_providers.render as render
    monkeypatch.setattr(render, "html_to_pdf", lambda body, base: _pdf_with(body))
    assert vnpt.VnptAdapter().fetch_pdf(VNPT_INV).startswith(b"%PDF")
    search = next(kw for m, u, kw in calls if u.endswith("/SearchInvoicebycode/Index"))
    assert search["data"]["strFkey"] == "7YZCACMAG*" and search["data"]["__RequestVerificationToken"] == "TOK"
    preview = next(kw for m, u, kw in calls if "ajxPreview" in u)
    assert preview["data"] == {"checkCode": "CHECK+CODE/1"}


def test_vnpt_refuses_non_4_digit_answer_then_gives_up(monkeypatch):
    calls = []
    _router(vnpt, monkeypatch, _vnpt_routes(_vnpt_result()), calls)
    monkeypatch.setattr(vnpt, "solve_captcha", lambda img, group, min_conf: "68867")
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(VNPT_INV)
    assert ei.value.kind == "captcha"
    assert not any(u.endswith("/SearchInvoicebycode/Index") for m, u, kw in calls)   # không gửi bừa


@pytest.mark.parametrize("msg, kind", [
    ("Mã xác nhận không đúng!", "captcha"),
    ("Không tồn tại hoá đơn!", "no_pdf"),                    # chữ "hoá" kiểu cũ
])
def test_vnpt_error_messages(monkeypatch, msg, kind):
    _router(vnpt, monkeypatch, _vnpt_routes(_vnpt_result(msg)), [])
    monkeypatch.setattr(vnpt, "solve_captcha", lambda img, group, min_conf: "1234")
    with pytest.raises(PdfFetchError) as ei:
        vnpt.VnptAdapter().fetch_pdf(VNPT_INV)
    assert ei.value.kind == kind
