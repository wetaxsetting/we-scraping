"""Adapter IGAS (tracuu.igas.vn, phần mềm VIET-INVOICE), HTTP giả lập — khảo sát 27/10/2026 (chưa có HĐ thật).

Run::

    pytest tests/test_pdf_igas.py -q
"""
from __future__ import annotations

import base64
import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402


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
    def __init__(self, status=200, content=b"", js=None):
        self.status_code, self.content, self._js = status, content, js
        self.headers, self.text = {}, content.decode("utf-8", "replace")

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.results.pop(0)


def _use(monkeypatch, mod, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(mod, "new_session", lambda: sess)
    return sess

from pdf_providers import igas  # noqa: E402

INV = {"shdon": 8, "khhdon": "C26TIG", "nbmst": "0101010101", "mst_tcgp": "0111090707",
       "ma_tra_cuu": "IGAS12345678", "tt_khac": {}}
XML = "<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>8</SHDon></TTChung></DLHDon></HDon>"
FOUND = {"result": "success", "data": {"invoiceId": "x", "xmlString": XML}}
NOT_FOUND = {"result": "failed", "message": "Không tìm thấy thông tin"}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0111090707").source_id == "igas"
    assert pdf_providers.adapter_for_host("tracuu.igas.vn").source_id == "igas"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, igas, _Resp(js=FOUND), _Resp(js={"fileURL": "/files/inv/8.pdf"}),
                _Resp(content=_pdf_with("So 8")))
    assert igas.IgasAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    _, url, kw = sess.calls[0]
    assert url == "https://hoadon.igas.vn/api/v1/misc/find-by-lookup-code"
    assert kw["params"] == {"lookupCode": "IGAS12345678"} and kw["headers"]["x-domain"] == "tracuu.igas.vn"
    assert sess.calls[1][1].endswith("/misc/export-by-lookup-code")
    assert sess.calls[1][2]["params"] == {"lookupCode": "IGAS12345678", "typeWebsite": 1}
    assert sess.calls[2][1] == "https://hoadon.igas.vn/files/inv/8.pdf"


def test_fetch_xml(monkeypatch):
    _use(monkeypatch, igas, _Resp(js=FOUND))
    assert igas.IgasAdapter().fetch_xml(dict(INV)) == XML.encode()
    _use(monkeypatch, igas, _Resp(js=FOUND))
    with pytest.raises(PdfFetchError) as ei:
        igas.IgasAdapter().fetch_xml(dict(INV, shdon=9))
    assert ei.value.kind == "bad_content"


def test_not_found(monkeypatch):
    _use(monkeypatch, igas, _Resp(400, js=NOT_FOUND))
    with pytest.raises(PdfFetchError) as ei:
        igas.IgasAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_not_pdf(monkeypatch):
    _use(monkeypatch, igas, _Resp(js=FOUND), _Resp(js={"fileURL": "/x"}), _Resp(content=b"<html>"))
    with pytest.raises(PdfFetchError) as ei:
        igas.IgasAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        igas.IgasAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = igas.IgasAdapter()
    assert a.invoice_from_link("https://tracuu.igas.vn/?lookupCode=AB12CD34EF", None, "")["ma_tra_cuu"] == "AB12CD34EF"
    assert a.invoice_from_link("https://tracuu.igas.vn/", None, "") is None
