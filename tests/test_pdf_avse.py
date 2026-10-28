"""Adapter Avsoft-Einvoice (hoadondientu.link), HTTP giả lập — khảo sát 27/10/2026, chưa có HĐ thật.

Run::

    pytest tests/test_pdf_avse.py -q
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


import requests  # noqa: E402

from pdf_providers import avse  # noqa: E402

INV = {"shdon": 5, "nbmst": "0302222222", "mst_tcgp": "0310768095", "tt_khac": {"Mã tra cứu": "AV5"}}
ERR500 = (b"<html><body><hgroup><h1>Error.</h1><h2>An error occurred while processing your request."
          b"</h2></hgroup></body></html>")


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(avse, "new_session", lambda: sess)
    return sess


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0310768095").source_id == "avse"
    assert pdf_providers.adapter_for_host("hoadondientu.link").source_id == "avse"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 5")))
    assert avse.AvseAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "https://hoadondientu.link/TraCuuTT78/GetInvoice"
    assert kw["data"] == {"maSoThue": "0302222222", "maBaoMat": "AV5"}


@pytest.mark.parametrize("result, kind", [
    (_Resp(500, ERR500, ctype="text/html"), "no_pdf"),          # phản hồi "không tìm thấy" thật
    (_Resp(503, b"Service Unavailable", ctype="text/html"), "server"),
    (_Resp(200, b"", ctype="text/html"), "no_pdf"),
    (_Resp(200, b"<html>khac</html>", ctype="text/html"), "bad_content"),
    (requests.Timeout("t"), "network"),
])
def test_errors(monkeypatch, result, kind):
    _use(monkeypatch, result)
    with pytest.raises(PdfFetchError) as ei:
        avse.AvseAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


@pytest.mark.parametrize("inv", [{"shdon": 1, "nbmst": "0302222222", "tt_khac": {}},
                                 {"shdon": 1, "nbmst": None, "ma_tra_cuu": "X"}])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        avse.AvseAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    assert avse.AvseAdapter().invoice_from_link("https://hoadondientu.link/tracuutt78", "X", "") is None
