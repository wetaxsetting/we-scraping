"""Adapter Nhóm Mây / Cloudteam, HTTP giả lập.

Run::

    pytest tests/test_pdf_cloudteam.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                   # noqa: E402
from pdf_providers import PdfFetchError               # noqa: E402
from pdf_providers import cloudteam, common           # noqa: E402

BASE = "https://chanel-eportal.cloudteam.vn"
INV = {"shdon": 2087, "nbmst": "0312617990", "tt_khac": {"MaTraCuu": "ABC123XYZ"},
       "ma_tra_cuu": "ABC123XYZ", "lookup_url": BASE}


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
    def __init__(self, status=200, content=b"", text=None, js=None):
        self.status_code, self.content, self._text, self._js = status, content, text, js
        self.headers = {"Content-Type": "application/pdf"}

    @property
    def text(self):
        return self._text if self._text is not None else self.content.decode("utf-8", "replace")

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


def _patch(monkeypatch, sess):
    monkeypatch.setattr(cloudteam, "new_session", lambda: sess)
    monkeypatch.setattr(cloudteam, "solve_captcha", lambda *a, **k: "00000")


def test_registry_host():
    assert pdf_providers.adapter_for_mst_tcgp("0312617990").source_id == "cloudteam"
    assert pdf_providers.adapter_for_host("chanel-eportal.cloudteam.vn").source_id == "cloudteam"


def test_fetch_ok(monkeypatch):
    sess = _Sess(
        _Resp(text='<meta name="csrf-token" content="TOK">'),       # GET /
        _Resp(js={"captcha": "<img src=\"/captcha/flat?seed\">"}),    # GET /refreshcaptcha
        _Resp(content=b"\x89PNG"),                                  # GET img
        _Resp(js={"linkFile": "/files/inv.pdf", "linkZip": "/files/inv.zip"}),  # POST check-invoice
        _Resp(content=_pdf_with("Hoa don so 2087")),               # GET linkFile
    )
    _patch(monkeypatch, sess)
    assert cloudteam.CloudteamAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    post = [c for c in sess.calls if c[0] == "POST"][0]
    assert '"InvNo": "ABC123XYZ"' in post[2]["data"]
    assert post[2]["headers"]["X-CSRF-TOKEN"] == "TOK"


def test_not_found(monkeypatch):
    sess = _Sess(
        _Resp(text='<meta name="csrf-token" content="TOK">'),
        _Resp(js={"captcha": "<img src=\"/captcha/flat?seed\">"}),
        _Resp(content=b"png"),
        _Resp(js={"message": "Không tìm thấy hóa đơn"}),
    )
    _patch(monkeypatch, sess)
    with pytest.raises(PdfFetchError) as e:
        cloudteam.CloudteamAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_wrong_captcha_retries(monkeypatch):
    def page():
        return [_Resp(text='<meta name="csrf-token" content="TOK">'),
                _Resp(js={"captcha": "<img src=\"/captcha/flat?s\">"}),
                _Resp(content=b"png"),
                _Resp(js={"errors": {"captcha": ["sai"]}, "message": "Vui lòng nhập đúng captcha"})]
    sess = _Sess(*(page() * cloudteam.CAPTCHA_TRIES))
    _patch(monkeypatch, sess)
    with pytest.raises(PdfFetchError) as e:
        cloudteam.CloudteamAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "captcha"


def test_no_code():
    with pytest.raises(PdfFetchError) as e:
        cloudteam.CloudteamAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert e.value.kind == "no_code"


def test_no_portal():
    inv = {"shdon": 1, "tt_khac": {"MaTraCuu": "X"}, "ma_tra_cuu": "X"}  # không có lookup_url cloudteam
    with pytest.raises(PdfFetchError) as e:
        cloudteam.CloudteamAdapter().fetch_pdf(inv)
    assert e.value.kind == "no_portal"


def test_invoice_from_link():
    inv = cloudteam.CloudteamAdapter().invoice_from_link(
        "https://chanel-eportal.cloudteam.vn/?key=MA123", None, "")
    assert inv["ma_tra_cuu"] == "MA123"
    assert inv["lookup_url"] == "https://chanel-eportal.cloudteam.vn"
    assert cloudteam.CloudteamAdapter().invoice_from_link("https://other.vn/?key=X", None, "") is None
