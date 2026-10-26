"""Adapter FAST e-Invoice (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 23/10/2026 trên 6 HĐ thật (C23TQA-473, C23TSH-13, C25TVC-30, C26TVC-2899,
K23TDP-238/842): cổng einvoice.fast.com.vn tải PDF ở t=4 với captcha RỖNG — server không kiểm
captcha ở bước tải, nên adapter không giải captcha. 6/6 ra PDF đúng số HĐ.

Run::

    pytest tests/test_pdf_fast.py -q
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
from pdf_providers import common           # noqa: E402
from pdf_providers import fast             # noqa: E402

KEY = "AQAAAAyUPYI5F/7IDvtx0mVgjPCCcihSeDgvQPkSCIp8DhSgu6rhxNP63KA=001068"
INV = {"shdon": 2899, "khhdon": "C26TVC", "khmshdon": 1, "nbmst": "0101871229",
       "tt_khac": {"KeySearch": KEY}}


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
    def __init__(self, content=b"", status=200, ctype=""):
        self.status_code, self.content = status, content
        self.headers = {"Content-Type": ctype}


def _setup(monkeypatch, calls, download):
    """download = phản hồi cho GET handler (t=4)."""
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url == fast.BASE + "/":
            return _Resp(b"<html/>", ctype="text/html")
        if url == fast.HANDLER:
            return download
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(fast, "request", fake)
    monkeypatch.setattr(fast, "new_session", lambda: object())


def test_ok_downloads_without_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, _Resp(_pdf_with("So 2899"), ctype="application/pdf"))
    assert fast.FastAdapter().fetch_pdf(INV).startswith(b"%PDF")
    # Gọi đúng handler t=4 với captcha rỗng, p = base64(KeySearch), n = SHDon
    g = [kw for m, u, kw in calls if u == fast.HANDLER][0]["params"]
    assert g["t"] == "4" and g["c"] == "" and g["n"] == "2899"
    assert base64.b64decode(g["p"]).decode() == KEY


def test_never_sends_a_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, _Resp(_pdf_with("So 2899"), ctype="application/pdf"))
    fast.FastAdapter().fetch_pdf(INV)
    assert all(kw["params"]["c"] == "" for m, u, kw in calls if u == fast.HANDLER)


def test_empty_body_is_no_pdf(monkeypatch):
    # Mã sai / HĐ không tồn tại → handler trả thân rỗng, không phải PDF.
    _setup(monkeypatch, [], _Resp(b"", ctype=""))
    with pytest.raises(PdfFetchError) as ei:
        fast.FastAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_html_body_is_no_pdf(monkeypatch):
    _setup(monkeypatch, [], _Resp(b"<html>loi</html>" + b"x" * 2000, ctype="text/html"))
    with pytest.raises(PdfFetchError) as ei:
        fast.FastAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_wrong_invoice_number_rejected(monkeypatch):
    _setup(monkeypatch, [], _Resp(_pdf_with("So 99999"), ctype="application/pdf"))
    with pytest.raises(PdfFetchError) as ei:
        fast.FastAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [
    {"shdon": 2899, "tt_khac": {}},                       # thiếu KeySearch
    {"shdon": 2899, "tt_khac": {}, "ma_tra_cuu": ""},
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        fast.FastAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_no_shdon():
    with pytest.raises(PdfFetchError) as ei:
        fast.FastAdapter().fetch_pdf({"shdon": "", "tt_khac": {"KeySearch": KEY}})
    assert ei.value.kind == "no_code"


def test_lookup_code_prefers_tt_khac():
    assert fast.FastAdapter.lookup_code({"tt_khac": {"KeySearch": " K1 "}, "ma_tra_cuu": "B"}) == "K1"
    assert fast.FastAdapter.lookup_code({"tt_khac": {}, "ma_tra_cuu": "Y2"}) == "Y2"


def test_invoice_from_link():
    a = fast.FastAdapter()
    got = a.invoice_from_link("https://einvoice.fast.com.vn/?c=" + KEY.replace("+", " "), None, "")
    assert got["tt_khac"]["KeySearch"] == KEY           # khoảng trắng trên URL = '+'
    assert a.invoice_from_link("https://einvoice.fast.com.vn/", None, "") is None
    assert a.invoice_from_link("https://einvoice.fast.com.vn/", "CODE1", "")["ma_tra_cuu"] == "CODE1"


@pytest.mark.parametrize("subject,shdon", [
    ("CÔNG TY CỔ PHẦN VCCORP : Thông báo phát hành hoá đơn điện tử số 5470 ", "5470"),
    ("Fast e-Invoice: Thông báo phát hành hóa đơn điện tử số 297 (Notification on Issuing E-Invoices No. 297)", "297"),
    ("Notification on Issuing E-Invoices No. 0004800", "4800"),
    ("Thông báo hóa đơn", None),
])
def test_invoice_from_link_takes_number_from_subject(subject, shdon):
    got = fast.FastAdapter().invoice_from_link("https://einvoice.fast.com.vn/index.aspx?c=" + KEY, None, subject)
    assert got.get("shdon") == shdon


def test_declares_mst_tcgp():
    a = fast.FastAdapter()
    assert a.source_id == "fast" and a.mst_tcgp == ("0100727825",)
    assert "einvoice.fast.com.vn" in a.hosts
