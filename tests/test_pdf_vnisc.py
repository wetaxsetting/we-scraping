"""Adapter VNISC / invoice.abcsys.vn (HTTP + ddddocr giả lập, không cần mạng).

Luồng thật đã kiểm 10/2026: 10/10 HĐ thật (BGT-, DAV-) tải được PDF, chữ trùng PDF trong mail;
ddddocr đọc captcha 6 chữ số được server nhận 20/20.

Run::

    pytest tests/test_pdf_vnisc.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import vnisc            # noqa: E402

SEARCH = vnisc.SEARCH
CAPTCHA_URL = vnisc.BASE + "/Capchar/ShowCaptchaImage"
PDF_PATH = "/Invoice/ViewPDF?InvoiceNumber=BGT2026EIS_00238805"


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
    def __init__(self, text="", status=200, content=b"", url=SEARCH, headers=None):
        self.text, self.status_code, self.content, self.url = text, status, content, url
        self.headers = headers or {}


def _err(msg):
    return _Resp("<script>$('#content-error').html('" + msg + "');</script>")


WRONG = _err("Mã xác nhận không chính xác. Xin vui lòng thử lại.")
NOT_FOUND = _err("Không tìm thấy số hóa đơn trùng khớp. Xin vui lòng thử lại")
FOUND = _Resp(f'<a href="/Invoice/DownloadXML?InvoiceNumber=X">XML</a><a href="{PDF_PATH}">PDF</a>',
              url=vnisc.BASE + "/Invoice/ViewInfomation")


def _setup(monkeypatch, calls, search, pdf=None, answers=("123456",)):
    search, answers = list(search), list(answers)
    pdf = pdf if pdf is not None else _Resp(content=_pdf_with("So 238805"), headers={"Content-Type": "application/pdf"})

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET" and url == SEARCH:
            return _Resp("<form/>")
        if url == CAPTCHA_URL:
            return _Resp(content=b"jpeg")
        if method == "POST" and url == SEARCH:
            return search.pop(0)
        if url == vnisc.BASE + PDF_PATH:
            return pdf
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(vnisc, "request", fake)
    monkeypatch.setattr(vnisc, "new_session", lambda: object())
    monkeypatch.setattr(vnisc, "ocr_text", lambda img: answers.pop(0) if len(answers) > 1 else answers[0])


INV = {"shdon": 238805, "khhdon": "C26TBV", "khmshdon": 1, "nbmst": "0312479363",
       "ma_tra_cuu": "BGT-F9WGNAKDCVYI", "tt_khac": {"MTCuu": "BGT-F9WGNAKDCVYI", "Agentcode": "BGT"}}


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [FOUND])
    assert vnisc.VniscAdapter().fetch_pdf(INV).startswith(b"%PDF")
    post = [kw for m, u, kw in calls if m == "POST"][0]
    assert post["data"] == {"txtSoHoaDon": "BGT-F9WGNAKDCVYI", "txtCapChar": "123456"}
    pdf_call = [kw for _, u, kw in calls if u.endswith(PDF_PATH)][0]
    assert pdf_call["headers"]["Referer"].endswith("/Invoice/ViewInfomation")


def test_accepts_db_shaped_invoice(monkeypatch):
    inv = dict(INV, tong_tien=Decimal("4572362"), ngay_lap=date(2026, 10, 8))
    _setup(monkeypatch, [], [FOUND])
    assert vnisc.VniscAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_retries_wrong_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [WRONG, FOUND])
    assert vnisc.VniscAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert sum(u == CAPTCHA_URL for _, u, _ in calls) == 2


def test_gives_up_after_tries(monkeypatch):
    _setup(monkeypatch, [], [WRONG] * vnisc.CAPTCHA_TRIES)
    with pytest.raises(PdfFetchError) as ei:
        vnisc.VniscAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"


def test_does_not_send_bad_answer(monkeypatch):
    """ddddocr đọc lệch độ dài → lấy captcha mới, không gửi bừa."""
    calls = []
    _setup(monkeypatch, calls, [FOUND], answers=("12a45", "123456"))
    assert vnisc.VniscAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert sum(m == "POST" for m, _, _ in calls) == 1
    assert sum(u == CAPTCHA_URL for _, u, _ in calls) == 2


def test_solve_keeps_digits_only(monkeypatch):
    monkeypatch.setattr(vnisc, "ocr_text", lambda img: " 26 3375")
    assert vnisc.solve(b"x") == "263375"


def test_not_found(monkeypatch):
    _setup(monkeypatch, [], [NOT_FOUND])
    with pytest.raises(PdfFetchError) as ei:
        vnisc.VniscAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


@pytest.mark.parametrize("pdf, kind", [
    (_Resp("<html>" + "x" * 2000, headers={"Content-Type": "text/html"}), "bad_content"),
    ("other_invoice", "bad_content"),
    (_Resp(status=404), "bad_content"),
])
def test_download_errors(monkeypatch, pdf, kind):
    if pdf == "other_invoice":
        pdf = _Resp(content=_pdf_with("So 99999"))
    _setup(monkeypatch, [], [FOUND], pdf=pdf)
    with pytest.raises(PdfFetchError) as ei:
        vnisc.VniscAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_result_page_without_link(monkeypatch):
    _setup(monkeypatch, [], [_Resp("<html>trang lạ</html>")])
    with pytest.raises(PdfFetchError) as ei:
        vnisc.VniscAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [
    {"shdon": 1, "ma_tra_cuu": "", "tt_khac": {}},
    {"shdon": 1, "ma_tra_cuu": "F9WGNAKDCVYI", "tt_khac": {}},          # thiếu tiền tố đại lý
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        vnisc.VniscAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = vnisc.VniscAdapter()
    inv = a.invoice_from_link("https://invoice.abcsys.vn/Invoice/Search?ID=%20BGT-PZZMTQSZQNLA", None, "")
    assert inv["ma_tra_cuu"] == "BGT-PZZMTQSZQNLA" and inv["tt_khac"]["MTCuu"] == "BGT-PZZMTQSZQNLA"
    assert a.invoice_from_link("https://invoice.abcsys.vn/Invoice/Search", "DAV-WU1PEOGBQUO9", "")["ma_tra_cuu"] \
        == "DAV-WU1PEOGBQUO9"
    assert a.invoice_from_link("https://invoice.abcsys.vn/Invoice/Search?ID=", None, "") is None


def test_declares_mst_tcgp():
    a = vnisc.VniscAdapter()
    assert a.source_id == "vnisc" and a.mst_tcgp == ("0102331762",) and a.hosts == ("invoice.abcsys.vn",)
