"""Adapter SaigonInvoice / eHCM (invoice.ehcm.vn), HTTP giả lập — khảo sát 27/10/2026, chưa có HĐ thật.

Run::

    pytest tests/test_pdf_ehcm.py -q
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


from pdf_providers import ehcm  # noqa: E402

INV = {"shdon": 4567, "nbmst": "0301111111", "mst_tcgp": "0310926922", "ma_tra_cuu": "EH12"}
PAGE = ('<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="VS" />'
        '<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="CA0B0334" />')


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(ehcm, "new_session", lambda: sess)
    return sess


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0310926922").source_id == "ehcm"
    assert pdf_providers.adapter_for_host("invoice.ehcm.vn").source_id == "ehcm"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _html(PAGE), _Resp(content=_pdf_with("So 4567")))
    assert ehcm.EhcmAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[1]
    assert method == "POST" and url == "https://invoice.ehcm.vn/"
    assert kw["data"]["txtSearch"] == "EH12" and kw["data"]["btnSearch"] == "Tra cứu"
    assert kw["data"]["__VIEWSTATEGENERATOR"] == "CA0B0334"


def test_not_found(monkeypatch):
    _use(monkeypatch, _html(PAGE),
         _html("<script>window.alert('Không có mã tra cứu này. Bạn xem lại')</script>"))
    with pytest.raises(PdfFetchError) as ei:
        ehcm.EhcmAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_wrong_invoice(monkeypatch):
    _use(monkeypatch, _html(PAGE), _Resp(content=_pdf_with("So 99999")))
    with pytest.raises(PdfFetchError) as ei:
        ehcm.EhcmAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        ehcm.EhcmAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = ehcm.EhcmAdapter()
    assert a.invoice_from_link("https://invoice.ehcm.vn/#TraCuu", "EH12", "")["ma_tra_cuu"] == "EH12"
    assert a.invoice_from_link("https://invoice.ehcm.vn/#TraCuu", None, "") is None
