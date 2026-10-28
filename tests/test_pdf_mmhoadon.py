"""Adapter MOMO E-INVOICE / tracuu.mmhoadon.vn (HTTP giả lập, không cần mạng).

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi "không tìm thấy" (data=0).

Run::

    pytest tests/test_pdf_mmhoadon.py -q
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
from pdf_providers import mmhoadon                                                  # noqa: E402


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


def _setup(monkeypatch, calls, routes):
    def fake(session, method, url, **kw):
        calls.append((url, kw.get("params")))
        for key, resp in routes.items():
            if key in url:
                return resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(mmhoadon, "request", fake)
    monkeypatch.setattr(mmhoadon, "new_session", lambda: object())


INV = {"shdon": 123, "nbmst": "0312345678", "ma_tra_cuu": None, "tt_khac": {"Mã tra cứu": "MM12345ABC"}}
FOUND = _Resp(data={"status_code": 200, "is_success": True, "message": "", "data": 777})
NOT_FOUND = _Resp(data={"status_code": 200, "is_success": True, "message": "", "data": 0})


def test_registry():
    assert adapter_for_mst_tcgp("0305289153").source_id == "mmhoadon"
    assert adapter_for_host("tracuu.mmhoadon.vn").source_id == "mmhoadon"


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {"/ma-tra-cuu/MM12345ABC": FOUND, "/v2/777/pdf": _Resp(content=_pdf_with("So 123"))})
    assert mmhoadon.MmHoaDonAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert calls[0][0] == f"{mmhoadon.API}/ma-tra-cuu/MM12345ABC"
    assert calls[1] == (f"{mmhoadon.API}/v2/777/pdf", {"ma_tra_cuu": "MM12345ABC", "page_size": 10})


def test_fetch_xml(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {"/ma-tra-cuu/": FOUND,
                                "/v2/777/download": _Resp(content=b"<?xml?><HDon><SHDon>123</SHDon></HDon>")})
    assert b"SHDon" in mmhoadon.MmHoaDonAdapter().fetch_xml(INV)
    assert calls[1][1] == {"ma_tra_cuu": "MM12345ABC"}


@pytest.mark.parametrize("routes, kind", [
    ({"/ma-tra-cuu/": NOT_FOUND}, "no_pdf"),
    ({"/ma-tra-cuu/": FOUND, "/pdf": _Resp(content=b"<html>" + b"x" * 2000)}, "bad_content"),
    ({"/ma-tra-cuu/": FOUND, "/pdf": _Resp(content=_pdf_with("So 999"))}, "bad_content"),
    ({"/ma-tra-cuu/": _Resp(400, data={"is_success": False, "message": "Có lỗi"})}, "other"),
])
def test_errors(monkeypatch, routes, kind):
    _setup(monkeypatch, [], routes)
    with pytest.raises(PdfFetchError) as ei:
        mmhoadon.MmHoaDonAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        mmhoadon.MmHoaDonAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = mmhoadon.MmHoaDonAdapter()
    assert a.invoice_from_link("https://tracuu.mmhoadon.vn/", None, "") is None
    assert a.invoice_from_link("https://tracuu.mmhoadon.vn/", "MM12345ABC", "")["ma_tra_cuu"] == "MM12345ABC"
    assert a.invoice_from_link("https://tracuu.mmhoadon.vn/tra-cuu?ma_tra_cuu=XY9876", None, "")["ma_tra_cuu"] \
        == "XY9876"
