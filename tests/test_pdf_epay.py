"""Adapter EPAY + ATOM (cổng tra cứu M-invoice white-label; HTTP giả lập, không cần mạng).

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi "không tìm thấy" (EPAY 400 code 1007,
ATOM 500 code 5000).

Run::

    pytest tests/test_pdf_epay.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError, adapter_for_host, adapter_for_mst_tcgp   # noqa: E402
from pdf_providers import atom, common, epay                                        # noqa: E402


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
        self.text = content.decode("latin-1")

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class _Sess:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params)


def _use(monkeypatch, handler):
    sess = _Sess(handler)
    monkeypatch.setattr(epay, "new_session", lambda: sess)
    return sess


INV = {"shdon": 88, "nbmst": "0101234567", "ma_tra_cuu": None, "tt_khac": {"Số bảo mật": "ABCD1234EF56"}}
EPAY_NF = _Resp(400, data={"code": 1007, "message": "Không tìm thấy hóa đơn/biên lai hợp lệ với thông tin đầu vào."})
ATOM_NF = _Resp(500, data={"code": "5000", "message": "Get Tenant fail because not found  Tenant with code"})


def test_registry():
    assert adapter_for_mst_tcgp("0108269133").source_id == "epay"
    assert adapter_for_mst_tcgp("0316887099").source_id == "atom"
    assert adapter_for_host("tracuuhoadon.epayjsc.com").source_id == "epay"
    assert adapter_for_host("tracuuhoadon.1invoice.vn").source_id == "atom"


@pytest.mark.parametrize("cls", [epay.EpayAdapter, atom.AtomAdapter])
def test_ok(monkeypatch, cls):
    sess = _use(monkeypatch, lambda url, p: _Resp(content=_pdf_with("So 88")))
    assert cls().fetch_pdf(INV).startswith(b"%PDF")
    url, params = sess.calls[0]
    assert url == f"{cls.BASE}/api/Search/SearchInvoice"
    assert params == {"masothue": "0101234567", "sobaomat": "ABCD1234EF56", "type": "PDF", "inchuyendoi": "false"}


def test_fetch_xml(monkeypatch):
    sess = _use(monkeypatch, lambda url, p: _Resp(content=b"<?xml version='1.0'?><HDon><SHDon>88</SHDon></HDon>"))
    assert b"SHDon" in epay.EpayAdapter().fetch_xml(INV)
    assert sess.calls[0] == (f"{epay.EpayAdapter.BASE}/api/Search/DownloadXml",
                             {"masothue": "0101234567", "sobaomat": "ABCD1234EF56"})


@pytest.mark.parametrize("resp, kind", [
    (EPAY_NF, "no_pdf"),
    (ATOM_NF, "no_pdf"),
    (_Resp(500, data={"code": "9999", "message": "boom"}), "server"),
    (_Resp(403, content=b"forbidden"), "other"),
    (_Resp(content=b"<html>" * 400), "bad_content"),
    (_Resp(content=_pdf_with("So 77")), "bad_content"),
])
def test_errors(monkeypatch, resp, kind):
    _use(monkeypatch, lambda url, p: resp)
    with pytest.raises(PdfFetchError) as ei:
        epay.EpayAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    a = epay.EpayAdapter()
    for inv in ({"shdon": 1, "nbmst": "0101234567", "tt_khac": {}}, dict(INV, nbmst="")):
        with pytest.raises(PdfFetchError) as ei:
            a.fetch_pdf(inv)
        assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = epay.EpayAdapter()
    inv = a.invoice_from_link("https://tracuuhoadon.epayjsc.com/?taxCode=0101234567&checkCode=ABCD1234&tab=invoice",
                              None, "")
    assert inv["nbmst"] == "0101234567" and inv["ma_tra_cuu"] == "ABCD1234"
    assert a.invoice_from_link("https://tracuuhoadon.epayjsc.com/", "ABCD1234", "") is None   # thiếu MST bán
