"""Adapter Sơn Phát (tracuuhoadon.sonphat.vn), HTTP giả lập.

Run::

    pytest tests/test_pdf_sonphat.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                   # noqa: E402
from pdf_providers import PdfFetchError                # noqa: E402
from pdf_providers import common, sonphat              # noqa: E402


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
        self.headers = {}

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


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(sonphat, "new_session", lambda: sess)
    return sess


API = "https://hoadon.sonphat.vn/api/v1"
INV = {"shdon": 12, "khhdon": "C26TSP", "ma_tra_cuu": "SP12ABC", "tt_khac": {}}
FOUND = _Resp(js={"result": "success", "data": {"token": "tok-1", "no": 12}})
NOT_FOUND = _Resp(404, js={"result": "failed", "message": ["Không tìm thấy thông tin"]})


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("4601328480").source_id == "sonphat"
    assert pdf_providers.adapter_for_host("tracuuhoadon.sonphat.vn").source_id == "sonphat"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, FOUND, _Resp(content=_pdf_with("So 12")))
    assert sonphat.SonPhatAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    assert sess.calls[0][1] == f"{API}/misc/find-by-lookup-code"
    assert sess.calls[0][2]["params"] == {"lookupCode": "SP12ABC"}
    assert sess.calls[1][1] == f"{API}/misc/export-by-lookup-code"
    assert sess.calls[1][2]["params"] == {"token": "tok-1"}


def test_ambiguous_picks_by_number(monkeypatch):
    amb = _Resp(400, js={"errorCode": "LOOKUP_AMBIGUOUS", "candidates": [
        {"token": "t-11", "no": 11, "serial": "1C26TSP"}, {"token": "t-12", "no": 12, "serial": "1C26TSP"}]})
    sess = _use(monkeypatch, amb, _Resp(content=_pdf_with("So 12")))
    sonphat.SonPhatAdapter().fetch_pdf(dict(INV))
    assert sess.calls[1][2]["params"] == {"token": "t-12"}


def test_ambiguous_without_match(monkeypatch):
    amb = _Resp(400, js={"errorCode": "LOOKUP_AMBIGUOUS", "candidates": [{"token": "t-11", "no": 11}]})
    _use(monkeypatch, amb)
    with pytest.raises(PdfFetchError) as e:
        sonphat.SonPhatAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_not_found(monkeypatch):
    _use(monkeypatch, NOT_FOUND)
    with pytest.raises(PdfFetchError) as e:
        sonphat.SonPhatAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_no_code():
    with pytest.raises(PdfFetchError) as e:
        sonphat.SonPhatAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert e.value.kind == "no_code"


def test_xml(monkeypatch):
    _use(monkeypatch, FOUND, _Resp(js={"result": "success", "data": "<HDon><SHDon>12</SHDon></HDon>"}))
    assert b"<SHDon>12" in sonphat.SonPhatAdapter().fetch_xml(dict(INV))


def test_invoice_from_link():
    a = sonphat.SonPhatAdapter()
    assert a.invoice_from_link("https://tracuuhoadon.sonphat.vn/?lookupCode=SP12ABC", None, "")["ma_tra_cuu"] == "SP12ABC"
    assert a.invoice_from_link("https://tracuuhoadon.sonphat.vn/", "XYZ12345", "")["ma_tra_cuu"] == "XYZ12345"
    assert a.invoice_from_link("https://tracuuhoadon.sonphat.vn/", None, "") is None
