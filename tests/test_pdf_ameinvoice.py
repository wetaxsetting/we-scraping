"""Adapter AMEINVOICE (NC9, ameinvoice.vn), HTTP giả lập — luồng thật đã kiểm 03/11/2026 trên C23TVN-424.

Run::

    pytest tests/test_pdf_ameinvoice.py -q
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
from pdf_providers import ameinvoice       # noqa: E402
from pdf_providers import common           # noqa: E402

KEY = "21271210042333867188"
INV = {"shdon": 424, "khhdon": "C23TVN", "khmshdon": 1, "nbmst": "3702473210", "tt_khac": {},
       "ma_tra_cuu": None, "lookup_url": f"https://ameinvoice.vn/einvoice/?key_receive={KEY}"}


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
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}


def _router(monkeypatch, resp, calls):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        return resp
    monkeypatch.setattr(ameinvoice, "request", fake)
    monkeypatch.setattr(ameinvoice, "new_session", lambda: object())


def test_registered():
    assert pdf_providers.adapter_for_mst_tcgp("0312270160").source_id == "ameinvoice"
    assert pdf_providers.adapter_for_host("ameinvoice.vn").source_id == "ameinvoice"


def test_ok_strips_bom(monkeypatch):
    calls = []
    pdf = _pdf_with("So 00000424")
    _router(monkeypatch, _Resp(content=b"\xef\xbb\xbf" + pdf, headers={"Content-Type": "application/pdf"}), calls)
    assert ameinvoice.AmeInvoiceAdapter().fetch_pdf(INV) == pdf
    _, url, kw = calls[0]
    assert url.endswith("/view-teamplate-invoice/")
    assert kw["params"] == {"down_pdf": "true", "key_receive": KEY}


def test_no_key():
    with pytest.raises(PdfFetchError) as e:
        ameinvoice.AmeInvoiceAdapter().fetch_pdf({**INV, "lookup_url": "https://ameinvoice.vn/"})
    assert e.value.kind == "no_code"


def test_vendor_db_error_is_server(monkeypatch):
    _router(monkeypatch, _Resp(content=b"\r\n\xef\xbb\xbfConnection Failed: 1049"), [])
    with pytest.raises(PdfFetchError) as e:
        ameinvoice.AmeInvoiceAdapter().fetch_pdf(INV)
    assert e.value.kind == "server"


def test_wrong_invoice_rejected(monkeypatch):
    _router(monkeypatch, _Resp(content=_pdf_with("So 00000999")), [])
    with pytest.raises(PdfFetchError) as e:
        ameinvoice.AmeInvoiceAdapter().fetch_pdf(INV)
    assert e.value.kind == "bad_content"


def test_invoice_from_link():
    a = ameinvoice.AmeInvoiceAdapter()
    url = ("https://ameinvoice.vn/einvoice/?lang=vi&key_receive=18083812062426714979"
           "&confirm_key=26714979202406121822205891CF78D813_0&is_confirm=1")
    inv = a.invoice_from_link(url, "GEZDAMJUHE3DENJSIBAEAQCA", "")
    assert inv["ma_tra_cuu"] == "18083812062426714979"
    assert a.lookup_key(inv) == "18083812062426714979"
    assert a.invoice_from_link("https://ameinvoice.vn/search-e-invoice-buyer?lang=vi", None, "") is None
    assert a.invoice_from_link("https://example.com/?key_receive=18083812062426714979", None, "") is None
