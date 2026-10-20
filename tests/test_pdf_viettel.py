"""Adapter Viettel S-Invoice (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm trên HĐ thật 10/2026 (C26TSA, C26TYY, C26MVV, C26TTG, C26MHO).

Run::

    pytest tests/test_pdf_viettel.py -q
"""

from __future__ import annotations

import sys
from decimal import Decimal
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import viettel          # noqa: E402


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
    def __init__(self, status=200, content=b"", js=None, headers=None):
        self.status_code, self.content = status, content
        self._js, self.headers = js, headers or {}

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


def _router(monkeypatch, routes, calls):
    """routes = list[(url_contains, resp)] — khớp theo thứ tự, mọi lệnh đều POST."""
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        for frag, resp in routes:
            if frag in url:
                return resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(viettel, "request", fake)
    monkeypatch.setattr(viettel, "new_session", lambda: object())


NOT_FOUND = _Resp(400, b"{}", js={"code": 400, "message": "INVOICE_NOT_FOUND",
                                  "data": "Không tìm thấy hóa đơn"})
V1_NO_MST = _Resp(400, b"{}", js={"code": 400, "message": "Bad Request",
                                  "data": "Không tìm thấy mã số thuế"})
CUR, V1 = "einvoicequery/sync/utility/downloadPDF", "invoice-v1/downloadPDF"

INV = {"shdon": 34983, "khhdon": "C26TSA", "khmshdon": "1", "nbmst": "0313592114",
       "ma_tra_cuu": "", "tt_khac": {"Trạng thái thanh toán": "Đã thanh toán",
                                     "Mã số bí mật": "UY0U5AFNXKNIXN3",
                                     "Mã số": "d4e3df7a-6c41-492c-b02a-20be1605e525"}}


def test_ok_posts_mst_and_secret_code(monkeypatch):
    calls = []
    _router(monkeypatch, [(CUR, _Resp(content=_pdf_with("So 34983")))], calls)
    assert viettel.ViettelAdapter().fetch_pdf(INV).startswith(b"%PDF")
    method, url, kw = calls[0]
    assert method == "POST" and len(calls) == 1
    assert kw["params"] == {"taxCode": "0313592114"}
    assert kw["json"]["supplierTaxCode"] == "0313592114"
    assert kw["json"]["reservationCode"] == "UY0U5AFNXKNIXN3"     # "Mã số bí mật", không phải "Mã số"


def test_accepts_db_shaped_invoice(monkeypatch):
    inv = dict(INV, khmshdon=1, tong_tien=Decimal("11532644"), ngay_lap=date(2026, 9, 18))
    _router(monkeypatch, [(CUR, _Resp(content=_pdf_with("So 34983")))], [])
    assert viettel.ViettelAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_falls_back_to_v1(monkeypatch):
    calls = []
    _router(monkeypatch, [(CUR, NOT_FOUND), (V1, _Resp(content=_pdf_with("So 34983")))], calls)
    assert viettel.ViettelAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert [V1 in u for _, u, _ in calls] == [False, True]


def test_not_found_on_both_is_no_pdf(monkeypatch):
    _router(monkeypatch, [(CUR, NOT_FOUND), (V1, V1_NO_MST)], [])
    with pytest.raises(PdfFetchError) as ei:
        viettel.ViettelAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


@pytest.mark.parametrize("resp, kind", [
    (_Resp(content=b""), "no_pdf"),                                       # chưa dựng xong
    (_Resp(content=b"<html>" + b"x" * 2000), "bad_content"),
    (_Resp(content=_pdf_with("So 99999")), "bad_content"),                # PDF của HĐ khác
    (_Resp(403, b"forbidden"), "other"),
    (_Resp(400, b"{}", js={"message": "Captcha không hợp lệ"}), "captcha"),
])
def test_errors(monkeypatch, resp, kind):
    _router(monkeypatch, [(CUR, resp)], [])
    with pytest.raises(PdfFetchError) as ei:
        viettel.ViettelAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


@pytest.mark.parametrize("inv", [
    {"shdon": 1, "nbmst": "0313592114", "tt_khac": {"Mã số": "d4e3df7a"}},     # chỉ có UUID
    {"shdon": 1, "nbmst": "", "tt_khac": {"Mã số bí mật": "ABC"}},             # thiếu MST bán
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        viettel.ViettelAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_lookup_code_tolerates_unicode_forms():
    import unicodedata
    key = unicodedata.normalize("NFD", "Mã Số Bí Mật")
    assert viettel.ViettelAdapter.lookup_code({"tt_khac": {key: " X1 "}}) == "X1"
    assert viettel.ViettelAdapter.lookup_code({"tt_khac": {}, "ma_tra_cuu": "Y2"}) == "Y2"


def test_registered_for_mst_tcgp():
    assert pdf_providers.get_adapter("viettel").mst_tcgp == ("0100109106",)
    assert pdf_providers.adapter_for_mst_tcgp("0100109106").source_id == "viettel"
