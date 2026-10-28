"""Adapter AITS (einvoice.aits.vn), HTTP giả lập.

Run::

    pytest tests/test_pdf_aits.py -q
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
from pdf_providers import aits, common                 # noqa: E402


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
    def __init__(self, status=200, content=b""):
        self.status_code, self.content = status, content
        self.headers = {}


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.results.pop(0)


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(aits, "new_session", lambda: sess)
    return sess


INV = {"shdon": 345, "nbmst": "0103019524", "tt_khac": {"Số bảo mật": "SBM12345"}}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0103019524").source_id == "aits"
    assert pdf_providers.adapter_for_host("einvoice.aits.vn").source_id == "aits"


def test_ok_pdf(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 345")))
    assert aits.AitsAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "GET" and url == "https://einvoice.aits.vn/api/Tracuu/E-Invoice"
    assert kw["params"] == {"mst": "0103019524", "id": "SBM12345"}


def test_ok_zip(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hd.pdf", _pdf_with("So 345"))
    _use(monkeypatch, _Resp(content=buf.getvalue()))
    assert aits.AitsAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")


def test_not_found(monkeypatch):
    _use(monkeypatch, _Resp(content=b"Value cannot be null.\r\nParameter name: content"))
    with pytest.raises(PdfFetchError) as e:
        aits.AitsAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_html_is_bad_content(monkeypatch):
    _use(monkeypatch, _Resp(content=b"<html>loi</html>"))
    with pytest.raises(PdfFetchError) as e:
        aits.AitsAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [{"shdon": 1, "nbmst": "0103019524", "tt_khac": {}},
                                 {"shdon": 1, "nbmst": "", "tt_khac": {"Số bảo mật": "SBM12345"}}])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as e:
        aits.AitsAdapter().fetch_pdf(inv)
    assert e.value.kind == "no_code"


def test_invoice_from_link():
    a = aits.AitsAdapter()
    inv = a.invoice_from_link("https://einvoice.aits.vn/api/Tracuu/E-Invoice?mst=0103019524&id=SBM12345", None, "")
    assert inv["nbmst"] == "0103019524" and inv["ma_tra_cuu"] == "SBM12345"
    assert a.invoice_from_link("https://einvoice.aits.vn/", "SBM12345", "") is None
