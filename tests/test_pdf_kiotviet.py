"""Adapter KiotViet (tracuuhoadon.kiotviet.vn), HTTP giả lập — luồng thật đã kiểm 27/10/2026 (link mail C26MTT-1479).

Run::

    pytest tests/test_pdf_kiotviet.py -q
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                   # noqa: E402
from pdf_providers import PdfFetchError                # noqa: E402
from pdf_providers import common, kiotviet             # noqa: E402


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
        return self.results.pop(0)


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(kiotviet, "new_session", lambda: sess)
    return sess


LINK = "https://einvoice-search.kiotapi.com/api/v1/downloadPdfInvoice?code=HD02&id=62a1-uuid&b=1&y=26"
ZLINK = "https://einvoice-search.kiotapi.com/api/v1/downloadPdfInvoice?code=HD07&id=62a1-uuid&b=1&y=26"
FOUND = {"status": "success", "code": 200, "message": "Tra cứu hóa đơn thành công",
         "data": {"pdf_file": {"link": LINK, "name": "invoice_0319245420_1479_15032026.pdf"},
                  "pdf_xml_zip": {"link": ZLINK, "name": "invoice_0319245420_1479_15032026.zip"}}}
NOT_FOUND = {"status": "success", "code": 200, "message": "Tra cứu hóa đơn thành công",
             "data": {"pdf_file": {"link": "", "name": ""}, "pdf_xml_zip": {"link": "", "name": ""}}}
INV = {"shdon": 1479, "khhdon": "C26MTT", "ma_tra_cuu": "81YGNI8VEF261479", "tt_khac": {}}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0104359717").source_id == "kiotviet"
    assert pdf_providers.adapter_for_host("tracuuhoadon.kiotviet.vn").source_id == "kiotviet"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _Resp(js=FOUND), _Resp(content=_pdf_with("So 1479")))
    assert kiotviet.KiotVietAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "https://api.einvoice.kiotviet.vn/api/einvoice/search"
    assert kw["json"] == {"signature": "81YGNI8VEF261479"}
    method, url, kw = sess.calls[1]
    assert method == "GET"
    assert url == "https://api.einvoice.kiotviet.vn/api/einvoice/download?code=HD02&id=62a1-uuid&b=1&y=26"


def test_xml_from_zip(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("invoice_0319245420_1479_15032026.xml", "<HDon><SHDon>1479</SHDon></HDon>")
        zf.writestr("invoice_0319245420_1479_15032026.pdf", _pdf_with("So 1479"))
    sess = _use(monkeypatch, _Resp(js=FOUND), _Resp(content=buf.getvalue()))
    assert b"<SHDon>1479" in kiotviet.KiotVietAdapter().fetch_xml(dict(INV))
    assert "code=HD07" in sess.calls[1][1]


def test_not_found_is_empty_link(monkeypatch):
    _use(monkeypatch, _Resp(js=NOT_FOUND))
    with pytest.raises(PdfFetchError) as e:
        kiotviet.KiotVietAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_wrong_invoice_pdf(monkeypatch):
    _use(monkeypatch, _Resp(js=FOUND), _Resp(content=_pdf_with("So 999")))
    with pytest.raises(PdfFetchError) as e:
        kiotviet.KiotVietAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as e:
        kiotviet.KiotVietAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}, "ma_cqt": "M1-26-D8QSF-00000001485"})
    assert e.value.kind == "no_code"


def test_invoice_from_link():
    a = kiotviet.KiotVietAdapter()
    inv = a.invoice_from_link("https://tracuuhoadon.kiotviet.vn/?shd=1479&mtc=81YGNI8VEF261479", None, "")
    assert inv["ma_tra_cuu"] == "81YGNI8VEF261479" and inv["shdon"] == 1479
    assert a.invoice_from_link("https://tracuuhoadon.kiotviet.vn/", "ABCDEF123456", "")["ma_tra_cuu"] == "ABCDEF123456"
    assert a.invoice_from_link("https://tracuuhoadon.kiotviet.vn/", None, "") is None
