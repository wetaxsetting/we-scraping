"""Adapter SUN Invoice / tracuu.suninvoice.vn (HTTP giả lập, không cần mạng).

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi 400 "Không tìm thấy hóa đơn".

Run::

    pytest tests/test_pdf_suninvoice.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError, adapter_for_host, adapter_for_mst_tcgp   # noqa: E402
from pdf_providers import common                                                    # noqa: E402
from pdf_providers import suninvoice                                                # noqa: E402

MCCQT = "006942132551964CC1A0E8111548F60F72"


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):
        page.insert_text((40, 40 + i * 18), f"{text} dong {chr(97 + i % 26)}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


class _Resp:
    def __init__(self, status=200, content=b"", data=None, headers=None):
        self.status_code, self.content, self._data = status, content, data
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


NOT_FOUND = _Resp(400, data={"errorCode": 400, "errors": "Không tìm thấy hóa đơn hoặc hóa đơn đã bị xóa"})


def _setup(monkeypatch, calls, routes):
    def fake(session, method, url, **kw):
        calls.append(url)
        for key, resp in routes.items():
            if url.endswith(key):
                return resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(suninvoice, "request", fake)
    monkeypatch.setattr(suninvoice, "new_session", lambda: object())


INV = {"shdon": 14, "nbmst": "0318117585", "ma_tra_cuu": None, "tt_khac": {"Mã tra cứu hóa đơn": "SUN12345CODE"},
       "ma_cqt": MCCQT}


def test_registry():
    assert adapter_for_mst_tcgp("0111488368").source_id == "suninvoice"
    assert adapter_for_host("tracuu.suninvoice.vn").source_id == "suninvoice"


def test_code_first_then_mccqt(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {"SUN12345CODE/pdf": NOT_FOUND, f"{MCCQT}/pdf": _Resp(content=_pdf_with("So 14"))})
    assert suninvoice.SunInvoiceAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert calls == [f"{suninvoice.API}/SUN12345CODE/pdf", f"{suninvoice.API}/{MCCQT}/pdf"]
    assert suninvoice.API == "https://tracuu.suninvoice.vn/api/rest/s1/iam-entry/invoices"


def test_fetch_xml(monkeypatch):
    _setup(monkeypatch, [], {"SUN12345CODE/xml": _Resp(content=b"<HDon><SHDon>00000014</SHDon></HDon>")})
    assert b"SHDon" in suninvoice.SunInvoiceAdapter().fetch_xml(INV)


@pytest.mark.parametrize("resp, kind", [
    (NOT_FOUND, "no_pdf"),
    (_Resp(content=b"<html>" + b"x" * 2000), "bad_content"),
    (_Resp(content=_pdf_with("So 99")), "bad_content"),
    (_Resp(404), "other"),
])
def test_errors(monkeypatch, resp, kind):
    _setup(monkeypatch, [], {"/pdf": resp})
    with pytest.raises(PdfFetchError) as ei:
        suninvoice.SunInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        suninvoice.SunInvoiceAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}, "ma_cqt": None})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = suninvoice.SunInvoiceAdapter()
    assert a.invoice_from_link("https://tracuu.suninvoice.vn/", None, "") is None
    assert a.invoice_from_link("https://tracuu.suninvoice.vn/", "SUN12345CODE", "")["ma_tra_cuu"] == "SUN12345CODE"
