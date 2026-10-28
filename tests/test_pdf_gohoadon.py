"""Adapter GO HÓA ĐƠN / tracuu.gohoadon.vn (HTTP giả lập, không cần mạng).

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi 400 "Không tìm thấy thông tin".

Run::

    pytest tests/test_pdf_gohoadon.py -q
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
from pdf_providers import gohoadon                                                  # noqa: E402


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
        calls.append((url, kw.get("params"), kw.get("headers")))
        for key, resp in routes.items():
            if key in url:
                return resp(kw.get("params")) if callable(resp) else resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(gohoadon, "request", fake)
    monkeypatch.setattr(gohoadon, "new_session", lambda: object())


INV = {"shdon": 45, "nbmst": "0312345678", "ma_tra_cuu": "GO123ABC", "tt_khac": {}}
NOT_FOUND = _Resp(400, data={"result": "failed", "message": "Không tìm thấy thông tin"})


def test_registry():
    assert adapter_for_mst_tcgp("0111245245").source_id == "gohoadon"
    assert adapter_for_host("tracuu.gohoadon.vn").source_id == "gohoadon"


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {"export-by-lookup-code": _Resp(data={"fileURL": "/files/x.pdf"}),
                                "/files/x.pdf": _Resp(content=_pdf_with("So 45"))})
    assert gohoadon.GoHoaDonAdapter().fetch_pdf(INV).startswith(b"%PDF")
    url, params, headers = calls[0]
    assert url == f"{gohoadon.API}/export-by-lookup-code"
    assert params == {"lookupCode": "GO123ABC", "typeWebsite": 1}
    assert headers["x-domain"] == "tracuu.gohoadon.vn"
    assert calls[1][0] == "https://hoadon.gohoadon.vn/files/x.pdf"


def test_second_code(monkeypatch):
    calls = []

    def export(params):
        return NOT_FOUND if params["lookupCode"] == "AAAA1111" else _Resp(data={"fileURL": "https://cdn.x/y.pdf"})
    _setup(monkeypatch, calls, {"export-by-lookup-code": export, "cdn.x/y.pdf": _Resp(content=_pdf_with("So 45"))})
    gohoadon.GoHoaDonAdapter().fetch_pdf(dict(INV, tt_khac={"Mã tra cứu": "AAAA1111"}))
    assert [c[1]["lookupCode"] for c in calls[:2]] == ["AAAA1111", "GO123ABC"]


def test_fetch_xml(monkeypatch):
    _setup(monkeypatch, [], {"find-by-lookup-code": _Resp(data={"data": {"xmlString": "<HDon><SHDon>45</SHDon></HDon>"}})})
    assert b"SHDon" in gohoadon.GoHoaDonAdapter().fetch_xml(INV)


@pytest.mark.parametrize("routes, kind", [
    ({"export-by-lookup-code": NOT_FOUND}, "no_pdf"),
    ({"export-by-lookup-code": _Resp(data={})}, "no_pdf"),
    ({"export-by-lookup-code": _Resp(data={"fileURL": "/a.pdf"}), "/a.pdf": _Resp(content=b"<html>" * 400)},
     "bad_content"),
    ({"export-by-lookup-code": _Resp(data={"fileURL": "/a.pdf"}), "/a.pdf": _Resp(content=_pdf_with("So 99"))},
     "bad_content"),
    ({"export-by-lookup-code": _Resp(403, data={})}, "other"),
])
def test_errors(monkeypatch, routes, kind):
    _setup(monkeypatch, [], routes)
    with pytest.raises(PdfFetchError) as ei:
        gohoadon.GoHoaDonAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        gohoadon.GoHoaDonAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = gohoadon.GoHoaDonAdapter()
    assert a.invoice_from_link("https://tracuu.gohoadon.vn/", None, "") is None
    assert a.invoice_from_link("https://tracuu.gohoadon.vn/?lookupCode=ABC12345", None, "")["ma_tra_cuu"] == "ABC12345"
