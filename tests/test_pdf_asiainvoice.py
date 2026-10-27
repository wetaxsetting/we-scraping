"""Adapter A-Invoice / ASIA asiainvoice.vn (HTTP + OCR giả lập, không cần mạng).

Luồng thật đã kiểm 26/10/2026: 2/2 HĐ Ricoh HCM (C26TRC-2258, 2377) từ mã trong mail → PDF + XML,
captcha ddddocr được server nhận 11/15.

Run::

    pytest tests/test_pdf_asiainvoice.py -q
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import asiainvoice      # noqa: E402
from pdf_providers import common           # noqa: E402

BASE = asiainvoice.BASE
FKEY = "OTIzd1NPMzAwMDAwMzY3NTIxMDg7OTIz"


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):
        page.insert_text((40, 40 + i * 18), f"{text} dong {chr(97 + i % 26)}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


class _Resp:
    def __init__(self, json=None, status=200, content=b""):
        self._json, self.status_code, self.content = json, status, content
        self.headers = {}

    def json(self):
        if self._json is None:
            raise ValueError("không phải JSON")
        return self._json


def _setup(monkeypatch, calls, answers, ocr=("ab1cd",), pdf=None, xml=b"<HDon><SHDon>2258</SHDon></HDon>"):
    """answers: list kết quả POST /tra-cuu lần lượt ("ok" | mã lỗi số)."""
    pdf = pdf if pdf is not None else _pdf_with("So 2258")
    answers, ocr = list(answers), list(ocr)

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url == BASE + "/tra-cuu/" or url == BASE + "/tra-cuu/GetCaptcha":
            return _Resp(content=b"img")
        if method == "POST" and url == BASE + "/tra-cuu":
            a = answers.pop(0)
            if a == "ok":
                return _Resp({"status": True, "pdf": base64.b64encode(pdf).decode(), "data": {}})
            return _Resp({"status": False, "error": a})
        if url == BASE + "/tra-cuu/downloadxml":
            return _Resp(content=xml)
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(asiainvoice, "request", fake)
    monkeypatch.setattr(asiainvoice, "new_session", lambda: object())
    monkeypatch.setattr(asiainvoice, "ocr_text", lambda img: ocr.pop(0) if len(ocr) > 1 else ocr[0])


INV = {"ma_tra_cuu": FKEY, "tt_khac": {"FKey": FKEY}, "shdon": "2258"}


def test_ok_upper_captcha_and_cached_xml(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, ["ok"])
    a = asiainvoice.AsiaInvoiceAdapter()
    assert b"<SHDon>2258</SHDon>" in a.fetch_xml(dict(INV))
    assert a.fetch_pdf(dict(INV)).startswith(b"%PDF")
    posts = [kw for m, u, kw in calls if m == "POST"]
    assert len(posts) == 1                                  # PDF lấy lại từ lần tra trước, không giải captcha 2 lần
    assert posts[0]["data"] == {"FKey": FKEY, "Captcha": "AB1CD"}


def test_retry_wrong_captcha_and_skip_bad_length(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [3, "ok"], ocr=["ab", "x1y2z", "q1w2e"])
    asiainvoice.AsiaInvoiceAdapter().fetch_pdf(dict(INV))
    assert [kw["data"]["Captcha"] for m, u, kw in calls if m == "POST"] == ["X1Y2Z", "Q1W2E"]


def test_captcha_exhausted(monkeypatch):
    _setup(monkeypatch, [], [3] * asiainvoice.MAX_TRIES)
    with pytest.raises(PdfFetchError) as ei:
        asiainvoice.AsiaInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "captcha"


def test_not_found(monkeypatch):
    _setup(monkeypatch, [], [1])
    with pytest.raises(PdfFetchError) as ei:
        asiainvoice.AsiaInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_pdf_of_other_invoice(monkeypatch):
    _setup(monkeypatch, [], ["ok"], pdf=_pdf_with("So 9999"))
    with pytest.raises(PdfFetchError) as ei:
        asiainvoice.AsiaInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_no_fkey_from_tax_xml():
    with pytest.raises(PdfFetchError) as ei:
        asiainvoice.AsiaInvoiceAdapter().fetch_pdf({"shdon": 2258, "tt_khac": {"TKHNbo": "WEBCASH"}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = asiainvoice.AsiaInvoiceAdapter()
    inv = a.invoice_from_link(f"https://tt78.asiainvoice.vn/EinvoiceView?token={FKEY}", None, "")
    assert inv["tt_khac"] == {"FKey": FKEY} and inv["mst_tcgp"] == "0101162173"
    assert a.invoice_from_link("https://asiainvoice.vn/tra-cuu/", FKEY, "")["ma_tra_cuu"] == FKEY
    assert a.invoice_from_link("https://tracuu.asiainvoice.vn", None, "") is None
    assert a.invoice_from_link("https://other.vn/?token=" + FKEY, FKEY, "") is None


def test_declares():
    a = asiainvoice.AsiaInvoiceAdapter()
    assert a.source_id == "asiainvoice" and a.mst_tcgp == ("0101162173",) and a.hosts == ("asiainvoice.vn",)
