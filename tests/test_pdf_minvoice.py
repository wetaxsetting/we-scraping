"""Adapter M-invoice (tracuuhoadon.minvoice.com.vn), HTTP giả lập — luồng thật đã kiểm 10/2026.

Run::

    pytest tests/test_pdf_minvoice.py -q
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                         # noqa: E402
from pdf_providers import PdfFetchError      # noqa: E402
from pdf_providers import common, minvoice   # noqa: E402


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
    def __init__(self, status=200, content=b"", js=None, ctype="application/pdf"):
        self.status_code, self.content, self._js = status, content, js
        self.headers = {"Content-Type": ctype}
        self.text = content.decode("utf-8", "replace")

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


class _Sess:
    def __init__(self, result):
        self.result, self.calls = result, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _use(monkeypatch, result) -> _Sess:
    sess = _Sess(result)
    monkeypatch.setattr(minvoice, "new_session", lambda: sess)
    return sess


INV = {"shdon": 11001, "khhdon": "C26TCA", "khmshdon": 1, "nbmst": "0104957749-001",
       "mst_tcgp": "0106026495", "ngay_lap": date(2026, 9, 15), "tong_tien": Decimal("2500000"),
       "tt_khac": {"Số bảo mật": "74AF34BF16B14504"}}
INV_MTT = {"shdon": 2703, "khhdon": "C26MBH", "khmshdon": "1", "nbmst": "0110097153",
           "mst_tcgp": "0106026495-001", "ngay_lap": "2026-09-22", "tong_tien": "1000",
           "loai_hd": "MTT",
           "tt_khac": {"Tỷ giá": "1", "Mã tra cứu": "DCCE75596C062723", "Địa chỉ": "x"}}
NOT_FOUND = {"code": "5000", "message": "Thông tin tra cứu không chính xác, vui lòng kiểm tra "
                                        "lại Mã số thuế và Số bảo mật.\n", "data": {"Code": 5000}}


def test_registry_maps_parent_and_branch():
    assert pdf_providers.adapter_for_mst_tcgp("0106026495").source_id == "minvoice"
    assert pdf_providers.adapter_for_mst_tcgp("0106026495-001").source_id == "minvoice"


def test_ok_sends_seller_mst_and_security_code(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 11001")))
    assert minvoice.MinvoiceAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "GET" and url.endswith("/api/Search/SearchInvoice")
    assert kw["params"] == {"masothue": "0104957749-001", "sobaomat": "74AF34BF16B14504",
                            "type": "PDF", "inchuyendoi": "false"}


def test_mtt_uses_ma_tra_cuu(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 2703")))
    assert minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_MTT)).startswith(b"%PDF")
    assert sess.calls[0][2]["params"]["sobaomat"] == "DCCE75596C062723"


@pytest.mark.parametrize("inv", [
    {"shdon": 1, "nbmst": "0104957749", "tt_khac": {}},
    {"shdon": 1, "nbmst": "", "tt_khac": {"Số bảo mật": "ABC"}},
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


@pytest.mark.parametrize("result, kind", [
    (_Resp(500, b"{}", NOT_FOUND, "application/json"), "no_pdf"),     # 500 nhưng là "không thấy"
    (_Resp(500, b"<html>loi</html>", None, "text/html"), "server"),
    (_Resp(502, b"{}", {"message": "Bad gateway"}, "application/json"), "server"),
    (_Resp(429, b""), "rate_limit"),
    (_Resp(400, b"{}", {"title": "validation"}, "application/problem+json"), "other"),
    (requests.Timeout("t"), "network"),
    (requests.ConnectionError("c"), "network"),
    (_Resp(200, b"<html>" + b"x" * 2000, None, "text/html"), "bad_content"),
    (_Resp(200, _pdf_with("So 99999")), "bad_content"),                 # PDF của HĐ khác
])
def test_errors(monkeypatch, result, kind):
    _use(monkeypatch, result)
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


MAIL_QUANNEM = "THÔNG BÁO XUẤT HÓA ĐƠN SỐ 2848\nE864CA9903996CF4\nMST người bán: 0313217924"


def test_invoice_from_link_mail():
    a = minvoice.MinvoiceAdapter()
    assert pdf_providers.adapter_for_host("tracuuhoadon.minvoice.vn") is pdf_providers.get_adapter("minvoice")
    inv = a.invoice_from_link("http://tracuuhoadon.minvoice.vn/", "E864CA9903996CF4", MAIL_QUANNEM)
    assert inv == {"nbmst": "0313217924", "ma_tra_cuu": "E864CA9903996CF4",
                   "tt_khac": {"Số bảo mật": "E864CA9903996CF4"}, "shdon": 2848}
    assert a.lookup_code(inv) == "E864CA9903996CF4"


@pytest.mark.parametrize("code, text", [
    (None, "THÔNG BÁO XUẤT HÓA ĐƠN SỐ 2848\nE864CA9903996CF4"),                      # thiếu MST người bán
    (None, "THÔNG BÁO XUẤT HÓA ĐƠN SỐ 2848\nMST người bán: 0313217924"),             # thiếu số bảo mật
    (None, MAIL_QUANNEM + "\n33197B32DEE9B3CA"),                                      # 2 mã → không đoán
])
def test_invoice_from_link_unusable(code, text):
    assert minvoice.MinvoiceAdapter().invoice_from_link("http://tracuuhoadon.minvoice.vn/", code, text) is None


# ---------------------------------------------------------------- cổng chung bắt reCAPTCHA (11/2026)
RECAPTCHA = {"success": False, "message": "Thiếu token Google reCAPTCHA."}


def test_public_recaptcha_goes_no_portal_and_stops_calling(monkeypatch):
    sess = _use(monkeypatch, _Resp(400, b"{}", RECAPTCHA, "application/json"))
    ad = minvoice.MinvoiceAdapter()
    for _ in range(3):
        with pytest.raises(PdfFetchError) as ei:
            ad.fetch_pdf(dict(INV))
        assert ei.value.kind == "no_portal"
        assert "MINVOICE_USER_" in str(ei.value)
    assert len(sess.calls) == 1                  # chỉ gọi cổng chung 1 lần mỗi tiến trình


# ---------------------------------------------------------------- tài khoản phát hành của người bán
SELLER = "0108515678-001"
INV_ACC = {"shdon": 2903, "khhdon": "C26TKC", "khmshdon": 1, "nbmst": SELLER, "mst_tcgp": "0106026495-001",
           "ngay_lap": date(2026, 11, 19), "tong_tien": Decimal("-1929000"),
           "dlhdon_id": "3a250436-acdc-ba3c-e55f-b1d956f57274",
           "tt_khac": {"Mã tra cứu": "BC3E0DF280B17177"}}
HOST = "https://0108515678-001.minvoice.com.vn"


class _Router:
    """Session giả: trả lời theo (method, đuôi URL); mỗi route là list kết quả, lấy lần lượt (giữ cái cuối)."""

    def __init__(self, routes):
        self.routes, self.calls = {k: list(v) for k, v in routes.items()}, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, tail), results in self.routes.items():
            if m == method and url.endswith(tail):
                res = results.pop(0) if len(results) > 1 else results[0]
                if isinstance(res, Exception):
                    raise res
                return res
        raise AssertionError(f"không ngờ gọi {method} {url}")


@pytest.fixture
def account(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("MINVOICE_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("INVOICE_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("MINVOICE_USER_0108515678_001", "ketoan")
    monkeypatch.setenv("MINVOICE_PASSWORD_0108515678_001", "mk-test")
    return tmp_path


def _route(monkeypatch, routes) -> _Router:
    r = _Router(routes)
    monkeypatch.setattr(minvoice, "new_session", lambda: r)
    return r


def _token(t="TK1"):
    return _Resp(200, b"{}", {"token": t}, "application/json")


def test_account_env_keys(account, monkeypatch):
    acct = minvoice.account_for(SELLER)
    assert acct == {"mst": SELLER, "username": "ketoan", "password": "mk-test", "ma_dvcs": "VP", "host": HOST}
    monkeypatch.setenv("MINVOICE_HOST_0108515678_001", "cuckoo.minvoice.app/")
    monkeypatch.setenv("MINVOICE_DVCS_0108515678_001", "CN1")
    acct = minvoice.account_for(SELLER)
    assert acct["host"] == "https://cuckoo.minvoice.app" and acct["ma_dvcs"] == "CN1"
    assert minvoice.account_for("0104957749") is None
    assert minvoice.configured_sellers() == ["0108515678_001"]


def test_account_prints_by_dlhdon_id(account, monkeypatch):
    r = _route(monkeypatch, {("POST", "/api/Account/Login"): [_token()],
                             ("GET", "/api/Invoice68/PrintInvoice"): [_Resp(content=_pdf_with("So 2903"))]})
    ad = minvoice.MinvoiceAdapter()
    assert ad.fetch_pdf(dict(INV_ACC)).startswith(b"%PDF")
    login, printed = r.calls
    assert login[1] == HOST + "/api/Account/Login"
    assert login[2]["json"] == {"username": "ketoan", "password": "mk-test", "ma_dvcs": "VP"}
    assert printed[2]["params"] == {"id": INV_ACC["dlhdon_id"], "type": "PDF", "inchuyendoi": "false"}
    assert printed[2]["headers"]["Authorization"] == "Bear TK1;VP;vi"
    ad.fetch_pdf(dict(INV_ACC))                                   # HĐ thứ 2: dùng lại token
    assert sum(c[1].endswith("/Account/Login") for c in r.calls) == 1
    assert not any("/Search/" in c[1] for c in r.calls)           # không đụng cổng tra cứu chung


def test_account_falls_back_to_grid_search(account, monkeypatch):
    grid = {"data": [{"id": "x1", "khieu": "1C26TKC", "shdon": 2902},
                     {"hoadon68_id": "GUID-2903", "khieu": "1C26TKC", "shdon": "2903"}]}
    r = _route(monkeypatch, {
        ("POST", "/api/Account/Login"): [_token()],
        ("GET", "/api/Invoice68/PrintInvoice"): [_Resp(200, b"{}", {"error": "khong tim thay"}, "application/json"),
                                                 _Resp(content=_pdf_with("So 2903"))],
        ("POST", "/api/Pattern/GetData"): [_Resp(200, b"{}", grid, "application/json")]})
    assert minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC)).startswith(b"%PDF")
    body = [c for c in r.calls if c[1].endswith("GetData")][0][2]["json"]
    assert body["command"] == "CM0009"
    assert {f["columnName"]: f["value"] for f in body["filter"]} == {"khieu": "C26TKC", "shdon": "2903"}
    assert r.calls[-1][2]["params"]["id"] == "GUID-2903"


def test_account_not_found_is_no_pdf(account, monkeypatch):
    _route(monkeypatch, {
        ("POST", "/api/Account/Login"): [_token()],
        ("GET", "/api/Invoice68/PrintInvoice"): [_Resp(200, b"{}", {"error": "x"}, "application/json")],
        ("POST", "/api/Pattern/GetData"): [_Resp(200, b"{}", {"data": []}, "application/json")]})
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC))
    assert ei.value.kind == "no_pdf"


def test_account_pdf_of_other_invoice_rejected(account, monkeypatch):
    _route(monkeypatch, {("POST", "/api/Account/Login"): [_token()],
                         ("GET", "/api/Invoice68/PrintInvoice"): [_Resp(content=_pdf_with("So 77777"))]})
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC))
    assert ei.value.kind == "bad_content"


def test_account_relogin_once_on_401(account, monkeypatch):
    r = _route(monkeypatch, {
        ("POST", "/api/Account/Login"): [_token("OLD"), _token("NEW")],
        ("GET", "/api/Invoice68/PrintInvoice"): [
            _Resp(401, b"Authorization has been denied for this request.", None, "text/plain"),
            _Resp(content=_pdf_with("So 2903"))]})
    assert minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC)).startswith(b"%PDF")
    auths = [c[2]["headers"]["Authorization"] for c in r.calls if c[1].endswith("PrintInvoice")]
    assert auths == ["Bear OLD;VP;vi", "Bear NEW;VP;vi"]


def test_wrong_password_is_blocked_across_processes(account, monkeypatch):
    denied = _Resp(200, b"{}", {"error": "Tên đăng nhập hoặc mật khẩu không đúng"}, "application/json")
    r = _route(monkeypatch, {("POST", "/api/Account/Login"): [denied]})
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC))
    assert ei.value.kind == "no_portal"
    blocks = (account / ".minvoice_login_blocks.json").read_text(encoding="utf-8")
    assert "mk-test" not in blocks                                    # không lưu mật khẩu
    with pytest.raises(PdfFetchError) as ei:                          # tiến trình mới, cùng mật khẩu
        minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC))
    assert ei.value.kind == "no_portal" and "không thử lại" in str(ei.value)
    assert len(r.calls) == 1                                          # không gọi M-invoice lần 2
    monkeypatch.setenv("MINVOICE_PASSWORD_0108515678_001", "mk-moi")  # đổi mật khẩu → được thử lại
    r2 = _route(monkeypatch, {("POST", "/api/Account/Login"): [_token()],
                              ("GET", "/api/Invoice68/PrintInvoice"): [_Resp(content=_pdf_with("So 2903"))]})
    assert minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC)).startswith(b"%PDF")
    assert len(r2.calls) == 2


@pytest.mark.parametrize("result, kind", [
    (requests.Timeout("t"), "network"),
    (_Resp(502, b"bad gateway", None, "text/html"), "server"),
    (_Resp(404, b"<html>", None, "text/html"), "server"),
])
def test_login_transient_errors_not_blocked(account, monkeypatch, result, kind):
    _route(monkeypatch, {("POST", "/api/Account/Login"): [result]})
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_ACC))
    assert ei.value.kind == kind
    assert not (account / ".minvoice_login_blocks.json").exists()


def test_has_pending_input_once_per_process(account, monkeypatch):
    ad = minvoice.MinvoiceAdapter()
    assert "0106026495-001" in ad.mst_tcgp          # requeue 'no_portal' khớp MSTTCGP chính xác
    assert ad.has_pending_input() is True
    assert ad.has_pending_input() is False
    monkeypatch.delenv("MINVOICE_USER_0108515678_001")
    assert minvoice.MinvoiceAdapter().has_pending_input() is False
