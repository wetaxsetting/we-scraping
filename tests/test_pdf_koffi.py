"""Adapter Koffi (ZAMO, api.koffi.vn), HTTP giả lập — khảo sát 27/10/2026, chưa có HĐ thật.

Run::

    pytest tests/test_pdf_koffi.py -q
"""
from __future__ import annotations

import sys
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
    def __init__(self, status=200, content=b"", js=None, ctype="application/pdf", url=""):
        self.status_code, self.content, self._js, self.url = status, content, js, url
        self.headers = {"Content-Type": ctype}
        self.text = content.decode("utf-8", "replace")

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


def _html(s: str) -> _Resp:
    return _Resp(content=s.encode(), ctype="text/html; charset=utf-8")


from pdf_providers import koffi  # noqa: E402

INV = {"shdon": 77, "nbmst": "0312345678", "mst_tcgp": "0313950909", "ma_tra_cuu": "Xy12AbCd",
       "tt_khac": {}}
DONE = {"status": "DONE", "url": "https://files.koffi.vn/v/1", "download_url": "https://files.koffi.vn/d/1"}


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(koffi, "new_session", lambda: sess)
    return sess


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0313950909").source_id == "koffi"
    assert pdf_providers.adapter_for_host("koffi.vn").source_id == "koffi"
    assert pdf_providers.adapter_for_host("api.koffi.vn").source_id == "koffi"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _Resp(js=DONE, ctype="application/json"), _Resp(content=_pdf_with("So 77")))
    assert koffi.KoffiAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "GET" and url == "https://api.koffi.vn/invoices/code/Xy12AbCd"
    assert kw["params"] == {"format": "pdf"}
    assert sess.calls[1][1] == "https://files.koffi.vn/d/1"


@pytest.mark.parametrize("resp, kind", [
    (_Resp(404, b'{"code":"E500"}', {"code": "E500", "message": "No entity tenant"}, "application/json"),
     "no_pdf"),
    (_Resp(200, b"{}", {"status": "NOT_FOUND"}, "application/json"), "no_pdf"),
    (_Resp(200, b"{}", {"status": "BUILDING"}, "application/json"), "server"),
    (_Resp(200, b"{}", {"status": "FAIL"}, "application/json"), "server"),
])
def test_not_ready_or_missing(monkeypatch, resp, kind):
    _use(monkeypatch, resp)
    with pytest.raises(PdfFetchError) as ei:
        koffi.KoffiAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


def test_not_pdf(monkeypatch):
    _use(monkeypatch, _Resp(js=DONE, ctype="application/json"), _Resp(content=b"<html>x</html>",
                                                                      ctype="text/html"))
    with pytest.raises(PdfFetchError) as ei:
        koffi.KoffiAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_fetch_xml(monkeypatch):
    xml = b"<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>77</SHDon></TTChung></DLHDon></HDon>"
    sess = _use(monkeypatch, _Resp(js=DONE, ctype="application/json"), _Resp(content=xml, ctype="text/xml"))
    assert koffi.KoffiAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[0][2]["params"] == {"format": "xml"}


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        koffi.KoffiAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = koffi.KoffiAdapter()
    assert a.invoice_from_link("https://koffi.vn/outbound/lookup-invoice/Ab12Cd34", None, "")["ma_tra_cuu"] \
        == "Ab12Cd34"
    assert a.invoice_from_link("https://koffi.vn/outbound/lookup-invoice", "QWE123", "")["ma_tra_cuu"] \
        == "QWE123"
    assert a.invoice_from_link("https://koffi.vn/outbound/lookup-invoice", None, "") is None
