"""Adapter MS-INVOICE (myinvoice.vn), HTTP giả lập — luồng thật đã kiểm 10/2026 trên C26TPF-18326.

Run::

    pytest tests/test_pdf_myinvoice.py -q
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import myinvoice        # noqa: E402

MCCQT = "00BC8BC49B3CB743FCB8ED08B6E21596F8"
INV = {"shdon": 18326, "khhdon": "C26TPF", "khmshdon": 1, "nbmst": "0319274661",
       "tt_khac": {}, "ma_tra_cuu": "", "ma_cqt": MCCQT, "tong_tien": Decimal("455000")}


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
    def __init__(self, status=200, content=b"", js=None, headers=None):
        self.status_code, self.content = status, content
        self._js, self.headers = js, headers or {}

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


NOT_FOUND = _Resp(400, js={"errorCode": 400,
                           "errors": "Không tìm thấy hóa đơn hoặc hóa đơn đã bị xóa\n"})


def _router(monkeypatch, handler, calls):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        return handler(url)
    monkeypatch.setattr(myinvoice, "request", fake)
    monkeypatch.setattr(myinvoice, "new_session", lambda: object())


def test_ok_by_mccqt(monkeypatch):
    calls = []
    pdf = _pdf_with("So 00018326")                          # số HĐ in kèm số 0 đầu vẫn khớp
    _router(monkeypatch, lambda url: _Resp(content=pdf, headers={"Content-Type": "application/pdf"}),
            calls)
    assert myinvoice.MyInvoiceAdapter().fetch_pdf(INV) == pdf
    assert [(m, u) for m, u, _ in calls] == [
        ("GET", f"https://tracuu.myinvoice.vn/erp/rest/s1/iam-entry/invoices/{MCCQT}/pdf")]


def test_tries_own_code_first_then_mccqt(monkeypatch):
    calls = []
    pdf = _pdf_with("HD 18326")
    _router(monkeypatch, lambda url: NOT_FOUND if "/MYCODE/" in url else _Resp(content=pdf), calls)
    inv = {**INV, "ma_tra_cuu": "MYCODE"}
    assert myinvoice.MyInvoiceAdapter().fetch_pdf(inv) == pdf
    assert ["/MYCODE/" in u for _, u, _ in calls] == [True, False]


def test_not_found_anywhere(monkeypatch):
    _router(monkeypatch, lambda url: NOT_FOUND, [])
    with pytest.raises(PdfFetchError) as ei:
        myinvoice.MyInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


@pytest.mark.parametrize("resp, kind", [
    (_Resp(content=b"<html>" + b"x" * 2000, headers={"Content-Type": "text/html"}), "bad_content"),
    (_Resp(status=404), "other"),
])
def test_errors(monkeypatch, resp, kind):
    _router(monkeypatch, lambda url: resp, [])
    with pytest.raises(PdfFetchError) as ei:
        myinvoice.MyInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_rejects_pdf_of_another_invoice(monkeypatch):
    _router(monkeypatch, lambda url: _Resp(content=_pdf_with("HD 99999")), [])
    with pytest.raises(PdfFetchError) as ei:
        myinvoice.MyInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


def test_without_any_code():
    with pytest.raises(PdfFetchError) as ei:
        myinvoice.MyInvoiceAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}, "ma_tra_cuu": "", "ma_cqt": ""})
    assert ei.value.kind == "no_code"


def test_registered_for_mst_tcgp():
    assert pdf_providers.get_adapter("myinvoice").mst_tcgp == ("0108971656",)
    assert pdf_providers.adapter_for_mst_tcgp("0108971656").source_id == "myinvoice"
