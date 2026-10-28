"""Adapter Home Casta (tracuu.homecasta.vn, cùng phần mềm VININVOICE), HTTP giả lập.

Run::

    pytest tests/test_pdf_homecasta.py -q
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
from pdf_providers import common, homecasta            # noqa: E402


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
        return self.results.pop(0)


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(homecasta, "new_session", lambda: sess)
    return sess


NOT_FOUND = _Resp(400, b"{}", js={"errorCode": 400, "errors": "Không tìm thấy hóa đơn có mã tra cứu: X\n"},
                  ctype="application/json")
API = "https://tracuu.homecasta.vn/api/rest/s1/iam-entry/invoices"


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0201802839").source_id == "homecasta"
    assert pdf_providers.adapter_for_host("tracuu.homecasta.vn").source_id == "homecasta"


def test_ok_falls_back_to_mccqt(monkeypatch):
    sess = _use(monkeypatch, NOT_FOUND, _Resp(content=_pdf_with("So 55")))
    inv = {"shdon": 55, "ma_tra_cuu": "94C2359A44624334", "ma_cqt": "00ABCDEF0123456789ABCDEF0123456789", "tt_khac": {}}
    assert homecasta.HomeCastaAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert [c[1] for c in sess.calls] == [f"{API}/94C2359A44624334/pdf",
                                          f"{API}/00ABCDEF0123456789ABCDEF0123456789/pdf"]


def test_not_found(monkeypatch):
    _use(monkeypatch, NOT_FOUND)
    with pytest.raises(PdfFetchError) as e:
        homecasta.HomeCastaAdapter().fetch_pdf({"shdon": 1, "ma_tra_cuu": "ABCDEF1234567890", "tt_khac": {}})
    assert e.value.kind == "no_pdf"


def test_no_code():
    with pytest.raises(PdfFetchError) as e:
        homecasta.HomeCastaAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert e.value.kind == "no_code"


def test_xml(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=b"<?xml version='1.0'?><HDon><SHDon>7</SHDon></HDon>", ctype="text/xml"))
    assert b"<SHDon>7" in homecasta.HomeCastaAdapter().fetch_xml({"shdon": 7, "ma_tra_cuu": "ABCDEF12", "tt_khac": {}})
    assert sess.calls[0][1] == f"{API}/ABCDEF12/xml"


def test_invoice_from_link():
    a = homecasta.HomeCastaAdapter()
    assert a.invoice_from_link("https://tracuu.homecasta.vn/#/", "94C2359A44624334", "")["ma_tra_cuu"] == "94C2359A44624334"
    assert a.invoice_from_link("https://tracuu.homecasta.vn/#/", None, "") is None
