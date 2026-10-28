"""Adapter Sapo Invoice (invoice.sapo.vn), HTTP giả lập — khảo sát 27/10/2026 (chưa có HĐ thật).

Run::

    pytest tests/test_pdf_sapo.py -q
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

from pdf_providers import sapo  # noqa: E402

INV = {"shdon": 55, "khhdon": "C26TSP", "nbmst": "0101010101", "mst_tcgp": "0103243195",
       "ma_tra_cuu": "SP123ABC456", "tt_khac": {}}
NOT_FOUND = {"errors": [{"message": "Không tìm thấy thông tin hóa đơn với mã tra cứu là X", "code": "not_found"}]}


def _ok(pdf: bytes) -> _Resp:
    return _Resp(js={"invoice": {"base64": base64.b64encode(pdf).decode()}})


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0103243195").source_id == "sapo"
    assert pdf_providers.adapter_for_host("www.sapo.vn").source_id == "sapo"
    assert pdf_providers.adapter_for_host("invoice.sapo.vn").source_id == "sapo"


def test_ok(monkeypatch):
    pdf = _pdf_with("So 55")
    sess = _use(monkeypatch, sapo, _ok(pdf))
    assert sapo.SapoAdapter().fetch_pdf(dict(INV)) == pdf
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "https://invoice.sapo.vn/api/invoices/look_up"
    assert kw["json"] == {"invoice": {"reference_code": "SP123ABC456"}}


def test_tries_next_code(monkeypatch):
    pdf = _pdf_with("So 55")
    inv = dict(INV, tt_khac={"Mã tra cứu": "OTHER9999"})
    sess = _use(monkeypatch, sapo, _Resp(404, js=NOT_FOUND), _ok(pdf))
    assert sapo.SapoAdapter().fetch_pdf(inv) == pdf
    assert sess.calls[1][2]["json"]["invoice"]["reference_code"] == "OTHER9999"


@pytest.mark.parametrize("resp", [
    _Resp(404, js=NOT_FOUND),
    _Resp(200, js={"errors": [{"fields": ["publish_status_waiting_for_response"]}]}),
])
def test_not_found(monkeypatch, resp):
    _use(monkeypatch, sapo, resp)
    with pytest.raises(PdfFetchError) as ei:
        sapo.SapoAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_server_error(monkeypatch):
    _use(monkeypatch, sapo, _Resp(502, b"bad gateway"))
    with pytest.raises(PdfFetchError) as ei:
        sapo.SapoAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "server"


def test_wrong_pdf(monkeypatch):
    _use(monkeypatch, sapo, _ok(_pdf_with("So 999")))
    with pytest.raises(PdfFetchError) as ei:
        sapo.SapoAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        sapo.SapoAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = sapo.SapoAdapter()
    inv = a.invoice_from_link("https://www.sapo.vn/tra-cuu-sapo-invoice.html?qrcode=ABCD12345", None, "")
    assert inv["ma_tra_cuu"] == "ABCD12345"
    assert a.invoice_from_link("https://www.sapo.vn/tra-cuu-sapo-invoice.html", None, "") is None
    assert a.invoice_from_link("https://www.sapo.vn/blog/khuyen-mai", "ABCD12345", "") is None
