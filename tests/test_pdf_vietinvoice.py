"""Adapter VIET-INVOICE / iCORP (tracuuhoadon.vietinvoice.vn + tracuu.vietinvoice.vn), HTTP giả lập —
chưa có HĐ thật (27/10/2026).

Run::

    pytest tests/test_pdf_vietinvoice.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                   # noqa: E402
from pdf_providers import PdfFetchError                # noqa: E402
from pdf_providers import common, vietinvoice          # noqa: E402


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

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        res = self.results.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(vietinvoice, "new_session", lambda: sess)
    return sess


def _J(js, status=200):
    return _Resp(status, b"{...}", js, "application/json")


NEW_NF = _J({"result": "failed", "message": "Không tìm thấy thông tin"}, 400)
OLD_NF = _J({"errorCode": 400, "errors": "Không tìm thấy hóa đơn có mã tra cứu: X"}, 400)
NEW_API = "https://hoadondientu.vietinvoice.vn/api/v1/misc"
OLD_API = "https://tracuu.vietinvoice.vn/erp/rest/s1/iam-entry/invoices"
MCCQT = "00ABCDEF0123456789ABCDEF0123456789"
INV = {"shdon": 123, "khhdon": "C25TAA", "mst_tcgp": "0106870211", "ma_tra_cuu": "94C2359A44624334",
       "ma_cqt": MCCQT, "tt_khac": {}}
XML = b"<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>123</SHDon></TTChung></DLHDon></HDon>"


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0106870211").source_id == "vietinvoice"
    assert pdf_providers.adapter_for_host("tracuu.vietinvoice.vn").source_id == "vietinvoice"
    assert pdf_providers.adapter_for_host("tracuuhoadon.vietinvoice.vn").source_id == "vietinvoice"
    assert pdf_providers.adapter_for_mst_tcgp("0109282176").source_id == "vininvoice"   # không cướp


def test_codes():
    inv = {"ma_tra_cuu": None, "ma_cqt": MCCQT, "tt_khac": {"Mã tra cứu": "AB12CD34", "Ghi chú": "x y"}}
    assert vietinvoice.VietInvoiceAdapter.secret_codes(inv) == ["AB12CD34"]
    assert vietinvoice.VietInvoiceAdapter.lookup_codes(inv) == ["AB12CD34", MCCQT]


def test_ok_new_portal(monkeypatch):
    sess = _use(monkeypatch, _J({"result": "success", "data": {"no": 123}}),
                _J({"fileURL": "/files/hd123.pdf"}), _Resp(content=_pdf_with("So 123")))
    assert vietinvoice.VietInvoiceAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    m, url, kw = sess.calls[0]
    assert url == f"{NEW_API}/find-by-lookup-code" and kw["params"] == {"lookupCode": "94C2359A44624334"}
    assert kw["headers"]["x-domain"] == "tracuuhoadon.vietinvoice.vn"
    assert sess.calls[1][2]["params"] == {"lookupCode": "94C2359A44624334", "typeWebsite": 1}
    assert sess.calls[2][1] == "https://hoadondientu.vietinvoice.vn/files/hd123.pdf"


def test_old_portal_then_mccqt(monkeypatch):
    sess = _use(monkeypatch, NEW_NF, OLD_NF, _Resp(content=_pdf_with("So 123")))
    assert vietinvoice.VietInvoiceAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    assert sess.calls[1][1] == f"{OLD_API}/94C2359A44624334/pdf"
    assert sess.calls[2][1] == f"{OLD_API}/{MCCQT}/pdf"


def test_not_found(monkeypatch):
    _use(monkeypatch, NEW_NF, OLD_NF, OLD_NF)
    with pytest.raises(PdfFetchError) as ei:
        vietinvoice.VietInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_new_only_code_not_found(monkeypatch):
    _use(monkeypatch, NEW_NF)          # mã có gạch ngang: cổng cũ không nhận dạng này → no_pdf
    with pytest.raises(PdfFetchError) as ei:
        vietinvoice.VietInvoiceAdapter().fetch_pdf({"shdon": 1, "ma_tra_cuu": "ab-12", "tt_khac": {}})
    assert ei.value.kind == "no_pdf"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        vietinvoice.VietInvoiceAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


@pytest.mark.parametrize("results, kind", [
    ((_Resp(500, b"x"),), "server"),
    ((requests.Timeout("t"),), "network"),
    ((_J({"result": "success", "data": {}}), _J({})), "no_pdf"),                       # không fileURL
    ((_J({"result": "success", "data": {}}), _J({"fileURL": "/a.pdf"}),
      _Resp(200, b"<html>" + b"x" * 2000, None, "text/html")), "bad_content"),
    ((_J({"result": "success", "data": {}}), _J({"fileURL": "/a.pdf"}),
      _Resp(content=_pdf_with("So 99999"))), "bad_content"),                            # PDF HĐ khác
    ((NEW_NF, _Resp(200, b"<html>" + b"x" * 2000, None, "text/html")), "bad_content"),  # cổng cũ
])
def test_errors(monkeypatch, results, kind):
    _use(monkeypatch, *results)
    with pytest.raises(PdfFetchError) as ei:
        vietinvoice.VietInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


def test_fetch_xml_new(monkeypatch):
    _use(monkeypatch, _J({"result": "success", "data": {"xmlString": XML.decode()}}))
    assert vietinvoice.VietInvoiceAdapter().fetch_xml(dict(INV)) == XML


def test_fetch_xml_old(monkeypatch):
    sess = _use(monkeypatch, NEW_NF, _Resp(content=XML, ctype="text/xml"))
    assert vietinvoice.VietInvoiceAdapter().fetch_xml(dict(INV)) == XML
    assert sess.calls[1][1] == f"{OLD_API}/94C2359A44624334/xml"
    _use(monkeypatch, NEW_NF, _Resp(content=XML.replace(b"123", b"999"), ctype="text/xml"))
    with pytest.raises(PdfFetchError) as ei:
        vietinvoice.VietInvoiceAdapter().fetch_xml(dict(INV))
    assert ei.value.kind == "bad_content"


def test_invoice_from_link():
    a = vietinvoice.VietInvoiceAdapter()
    assert a.invoice_from_link("https://tracuuhoadon.vietinvoice.vn/?lookupCode=K8X2M9Q1", None, "")[
        "ma_tra_cuu"] == "K8X2M9Q1"
    assert a.invoice_from_link("https://tracuu.vietinvoice.vn/#/", "94C2359A44624334", "")["ma_tra_cuu"] \
        == "94C2359A44624334"
    assert a.invoice_from_link("https://tracuu.vietinvoice.vn/#/", None, "") is None
