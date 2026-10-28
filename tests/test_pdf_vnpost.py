"""Adapter VNPost / tracuuhoadon.vnpost.vn (HTTP giả lập, không cần mạng).

Khảo sát 27/10/2026: "Mã xác thực" chỉ sinh + so ở trình duyệt; chưa có HĐ thật — chỉ kiểm phản
hồi 404 "Không tìm thấy hóa đơn".

Run::

    pytest tests/test_pdf_vnpost.py -q
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError, adapter_for_host, adapter_for_mst_tcgp   # noqa: E402
from pdf_providers import common                                                    # noqa: E402
from pdf_providers import vnpost                                                    # noqa: E402


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


NOT_FOUND = _Resp(404, data={"success": False, "message": "Không tìm thấy hóa đơn, vui lòng kiểm tra và thử lại.",
                             "data": None})
INV = {"shdon": 321, "nbmst": "0102595740", "ma_tra_cuu": "VNP12345", "tt_khac": {}}


def _setup(monkeypatch, calls, routes):
    def fake(session, method, url, **kw):
        calls.append((url, kw.get("params")))
        for key, resp in routes.items():
            if key in url:
                return resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(vnpost, "request", fake)
    monkeypatch.setattr(vnpost, "new_session", lambda: object())


def test_registry():
    assert adapter_for_mst_tcgp("0102595740").source_id == "vnpost"
    assert adapter_for_host("tracuuhoadon.vnpost.vn").source_id == "vnpost"


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {"/VNP12345/pdf": _Resp(content=_pdf_with("So 321"))})
    assert vnpost.VnPostAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert calls == [("https://tracuuhoadon.vnpost.vn/invoice-lookup/api/invoices/VNP12345/pdf", None)]


def test_fetch_xml(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {"/lookup": _Resp(data={"success": True, "totalRecord": 1,
                                                       "data": [{"number": 321, "xml": "<HDon><SHDon>321</SHDon></HDon>"}]})})
    assert b"SHDon" in vnpost.VnPostAdapter().fetch_xml(INV)
    assert calls[0][1] == {"key": "VNP12345"}


def test_fetch_xml_not_found(monkeypatch):
    _setup(monkeypatch, [], {"/lookup": NOT_FOUND})
    with pytest.raises(PdfFetchError) as ei:
        vnpost.VnPostAdapter().fetch_xml(INV)
    assert ei.value.kind == "no_pdf"


@pytest.mark.parametrize("resp, kind", [
    (NOT_FOUND, "no_pdf"),
    (_Resp(content=b"<html>" * 400), "bad_content"),
    (_Resp(content=_pdf_with("So 999")), "bad_content"),
    (_Resp(403, data={"message": "Forbidden"}), "other"),
])
def test_errors(monkeypatch, resp, kind):
    _setup(monkeypatch, [], {"/pdf": resp})
    with pytest.raises(PdfFetchError) as ei:
        vnpost.VnPostAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        vnpost.VnPostAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def _jwt(payload: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")   # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(payload)}.sig"


def test_invoice_from_link():
    a = vnpost.VnPostAdapter()
    tok = _jwt({"invoiceKey": "KEY98765"})
    assert vnpost.jwt_invoice_key(tok) == "KEY98765"
    assert vnpost.jwt_invoice_key("rác") == ""
    assert a.invoice_from_link(f"https://tracuuhoadon.vnpost.vn/?tab=invoice&token={tok}", None, "")["ma_tra_cuu"] \
        == "KEY98765"
    assert a.invoice_from_link("https://tracuuhoadon.vnpost.vn/", None, "") is None
    assert a.invoice_from_link("https://tracuuhoadon.vnpost.vn/", "VNP12345", "")["ma_tra_cuu"] == "VNP12345"
