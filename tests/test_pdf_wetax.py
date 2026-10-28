"""Adapter WeTax / api.wetax.com.vn (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 27/10/2026: link tải pdf/xml (v1 và v2) trong mail Ly Thái Bakery trả PDF + XML.

Run::

    pytest tests/test_pdf_wetax.py -q
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
from pdf_providers import wetax            # noqa: E402

HASH = "0123456789abcdef0123456789abcdef"     # giả
LINK = f"https://api.wetax.com.vn/api/wtx/ca/v1/download/pdf2/733508/{HASH}"


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


def _fake(monkeypatch, resp):
    calls = []

    def fake(session, method, url, **kw):
        calls.append(url)
        return resp(url) if callable(resp) else resp
    monkeypatch.setattr(wetax, "request", fake)
    monkeypatch.setattr(wetax, "new_session", lambda: object())
    return calls


def test_registered_by_host():
    assert pdf_providers.adapter_for_host("api.wetax.com.vn").source_id == "wetax"
    assert pdf_providers.adapter_for_host("wetax.com.vn").source_id == "wetax"


@pytest.mark.parametrize("url, expected", [
    (LINK, {"wetax_ver": "2", "wetax_id": "733508", "wetax_hash": HASH}),
    (f"https://api.wetax.com.vn/api/wtx/ca/v1/download/xml/111350/{HASH.upper()}",
     {"wetax_ver": "", "wetax_id": "111350", "wetax_hash": HASH}),
    ("https://wetax.com.vn/tra-cuu?lookUpCode=W-9cYRr1qZkmY=", None),      # form tra có captcha
    ("https://api.wetax.com.vn/api/wtx/ca/v1/download/pdf2/733508/abc", None),
    ("https://evil.vn/api/wtx/ca/v1/download/pdf2/733508/" + HASH, None),
])
def test_invoice_from_link(url, expected):
    assert wetax.WetaxAdapter().invoice_from_link(url, None, "") == expected


def test_fetch_pdf_and_xml_use_same_version(monkeypatch):
    calls = _fake(monkeypatch, lambda url: _Resp(content=_pdf_with("So 132385") if "/pdf" in url
                                                 else b"<HDon><DLHDon Id='x'></DLHDon></HDon>"))
    a = wetax.WetaxAdapter()
    inv = a.invoice_from_link(LINK, None, "")
    assert common.is_pdf(a.fetch_pdf(dict(inv, shdon=132385)))
    assert a.fetch_xml(dict(inv)).startswith(b"<HDon")
    assert calls == [LINK, LINK.replace("/pdf2/", "/xml2/")]


def test_wrong_invoice_number_rejected(monkeypatch):
    _fake(monkeypatch, _Resp(content=_pdf_with("So 999")))
    inv = wetax.WetaxAdapter().invoice_from_link(LINK, None, "")
    with pytest.raises(PdfFetchError) as ei:
        wetax.WetaxAdapter().fetch_pdf(dict(inv, shdon=132385))
    assert ei.value.kind == "bad_content"


def test_not_found_and_no_code(monkeypatch):
    _fake(monkeypatch, _Resp(status=404))
    inv = wetax.WetaxAdapter().invoice_from_link(LINK, None, "")
    with pytest.raises(PdfFetchError) as ei:
        wetax.WetaxAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_pdf"
    with pytest.raises(PdfFetchError) as ei:
        wetax.WetaxAdapter().fetch_pdf({"shdon": 1})
    assert ei.value.kind == "no_code"
