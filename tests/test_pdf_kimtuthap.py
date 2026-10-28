"""Adapter Kim Tự Tháp (e-invoices.vn, PSCForm), HTTP giả lập.

Run::

    pytest tests/test_pdf_kimtuthap.py -q
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
from pdf_providers import common, kimtuthap            # noqa: E402


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
    def __init__(self, status=200, content=b"", text=None):
        if text is not None:
            content = text.encode("utf-8")
        self.status_code, self.content = status, content
        self.text = content.decode("utf-8", "replace")
        self.headers = {}
        self.url = "https://e-invoices.vn/User/Search?CurrSecretCodeSearchStr=x"


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.results.pop(0)


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(kimtuthap, "new_session", lambda: sess)
    return sess


GUID = "3f2b8c1e-9a4d-4e7b-8c21-5d6e7f8a9b0c"
INV = {"shdon": 88, "ma_tra_cuu": None, "tt_khac": {"Secret code": GUID}}
FOUND = ('<main><button id="aViewPdf">Xem</button>'
         '<iframe id="pdfViewer" hidden data-src="/User/ViewPdf?code=abc&amp;x=1"></iframe></main>')
NOT_FOUND = '<main><div class="text-danger validation-summary-errors"><ul><li>Invalid secret code!</li></ul></div></main>'


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0303549303").source_id == "kimtuthap"
    assert pdf_providers.adapter_for_host("e-invoices.vn").source_id == "kimtuthap"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _Resp(text=FOUND), _Resp(content=_pdf_with("So 88")))
    assert kimtuthap.KimTuThapAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert url == "https://e-invoices.vn/User/Search" and kw["params"] == {"CurrSecretCodeSearchStr": GUID}
    assert sess.calls[1][1] == "https://e-invoices.vn/User/ViewPdf?code=abc&x=1"


def test_not_found(monkeypatch):
    _use(monkeypatch, _Resp(text=NOT_FOUND))
    with pytest.raises(PdfFetchError) as e:
        kimtuthap.KimTuThapAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_page_without_pdf_link(monkeypatch):
    _use(monkeypatch, _Resp(text="<main><p>?</p></main>"))
    with pytest.raises(PdfFetchError) as e:
        kimtuthap.KimTuThapAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as e:
        kimtuthap.KimTuThapAdapter().fetch_pdf({"shdon": 1, "ma_tra_cuu": "ABC123", "tt_khac": {}})
    assert e.value.kind == "no_code"


def test_invoice_from_link():
    a = kimtuthap.KimTuThapAdapter()
    inv = a.invoice_from_link(f"https://e-invoices.vn/User/Search?CurrSecretCodeSearchStr={GUID}", None, "")
    assert inv["ma_tra_cuu"] == GUID
    assert a.invoice_from_link("https://e-invoices.vn/", GUID.upper(), "")["ma_tra_cuu"] == GUID.upper()
    assert a.invoice_from_link("https://e-invoices.vn/", None, "") is None
