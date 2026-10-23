"""Adapter SmartSign / Vi Na (HTTP + ddddocr giả lập, không cần mạng).

Luồng thật đã kiểm 10/2026: 4/4 HĐ có mã đúng tải được PDF (chữ trùng PDF trong mail), thêm
C26MYV-505 chưa có PDF nào. ddddocr sau khi bỏ sọc đỏ được server nhận 54/60 captcha.
Người bán 0304892753 (C24TNP) ghi trong XML một mã khác mã in trên PDF → cổng báo không thấy.

Run::

    pytest tests/test_pdf_smartsign.py -q
"""

from __future__ import annotations

import io
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
from pdf_providers import smartsign        # noqa: E402

B = smartsign.BASE
F = "ctl00$ContentPlaceHolder1$"


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
    def __init__(self, text="", status=200, content=b"", headers=None):
        self.text, self.status_code, self.content = text, status, content
        self.headers = headers or {}


def _page(vs="VS0", err="", found="", not_found=False):
    return _Resp(
        f'<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />'
        f'<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="{vs}" />'
        f'<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="EV-{vs}" />'
        f'<label id="ContentPlaceHolder1_lblXML" style="visibility: hidden;">{found}</label>'
        f'<div id="divNotFoundActions" style="text-align: center; display:{"flex" if not_found else "none"};">'
        f'</div><span id="ContentPlaceHolder1_tabErrContent">{err}</span>')


HOME = _page()
WRONG = _page("VS1", err="M&#227; captcha kh&#244;ng đ&#250;ng", not_found=True)
NOT_FOUND = _page("VS1", not_found=True)
FOUND = _page("VS2", found="26K8049787RX")


def _setup(monkeypatch, calls, search, download=None, answers=("ec87",)):
    search, answers = list(search), list(answers)
    download = download if download is not None else _Resp(
        content=_pdf_with("So 730"), headers={"Content-Type": "application/pdf"})

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET" and url == B:
            return HOME
        if url == B + "Pages/CImage.aspx":
            return _Resp(content=b"jpeg")
        if method == "POST" and url == B:
            fields = {k: v[1] for k, v in kw["files"].items()}
            if fields.get("__EVENTTARGET"):
                return download
            return search.pop(0)
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(smartsign, "request", fake)
    monkeypatch.setattr(smartsign, "new_session", lambda: object())
    monkeypatch.setattr(smartsign, "clean_captcha", lambda img: img)
    monkeypatch.setattr(smartsign, "ocr_text", lambda img: answers.pop(0) if len(answers) > 1 else answers[0])


INV = {"shdon": 730, "khhdon": "C26THT", "khmshdon": 1, "nbmst": "0318684922", "ma_tra_cuu": None,
       "tt_khac": {"Matracuu": "26K8049787RX", "Trangtracuu": "tracuuhd.smartsign.com.vn"}}


def _posts(calls):
    return [{k: v[1] for k, v in kw["files"].items()} for m, u, kw in calls if m == "POST"]


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [FOUND])
    assert smartsign.SmartSignAdapter().fetch_pdf(INV).startswith(b"%PDF")
    search, dl = _posts(calls)
    assert search[F + "txtCode"] == "26K8049787RX" and search[F + "txtCapcha"] == "ec87"
    assert search["Action"] == "SHOW_INVOICE" and search["__VIEWSTATE"] == "VS0"
    assert dl["__EVENTTARGET"] == F + "LinkButtonDownloadPDF"
    assert dl["__VIEWSTATE"] == "VS2"                       # viewstate của trang kết quả


def test_accepts_db_shaped_invoice(monkeypatch):
    inv = dict(INV, tong_tien=Decimal("216000"), ngay_lap=date(2026, 7, 8))
    _setup(monkeypatch, [], [FOUND])
    assert smartsign.SmartSignAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_keeps_case_of_answer(monkeypatch):
    """Server phân biệt hoa/thường: không được hạ chữ."""
    calls = []
    _setup(monkeypatch, calls, [FOUND], answers=("VR",))
    smartsign.SmartSignAdapter().fetch_pdf(INV)
    assert _posts(calls)[0][F + "txtCapcha"] == "VR"


def test_retries_wrong_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [WRONG, FOUND])
    assert smartsign.SmartSignAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert sum(u.endswith("CImage.aspx") for _, u, _ in calls) == 2


def test_gives_up_after_tries(monkeypatch):
    _setup(monkeypatch, [], [WRONG] * smartsign.CAPTCHA_TRIES)
    with pytest.raises(PdfFetchError) as ei:
        smartsign.SmartSignAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"


def test_does_not_send_bad_answer(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [FOUND], answers=("w", "abcdefgh", "ge8w"))
    smartsign.SmartSignAdapter().fetch_pdf(INV)
    assert len(_posts(calls)) == 2                          # 1 tra cứu + 1 tải
    assert _posts(calls)[0][F + "txtCapcha"] == "ge8w"


def test_not_found(monkeypatch):
    _setup(monkeypatch, [], [NOT_FOUND])
    with pytest.raises(PdfFetchError) as ei:
        smartsign.SmartSignAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_other_error_message(monkeypatch):
    _setup(monkeypatch, [], [_page("VS1", err="Lỗi hệ thống")])
    with pytest.raises(PdfFetchError) as ei:
        smartsign.SmartSignAdapter().fetch_pdf(INV)
    assert ei.value.kind == "other"


@pytest.mark.parametrize("download, kind", [
    (_Resp("<html>" + "x" * 2000, headers={"Content-Type": "text/html"}), "bad_content"),
    ("other_invoice", "bad_content"),
])
def test_download_errors(monkeypatch, download, kind):
    if download == "other_invoice":
        download = _Resp(content=_pdf_with("So 99999"))
    _setup(monkeypatch, [], [FOUND], download=download)
    with pytest.raises(PdfFetchError) as ei:
        smartsign.SmartSignAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_clean_captcha_drops_red_lines():
    from PIL import Image
    im = Image.new("RGB", (10, 4), (255, 255, 255))
    im.putpixel((1, 1), (255, 0, 0))       # sọc đỏ
    im.putpixel((2, 2), (20, 20, 20))      # nét chữ
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = Image.open(io.BytesIO(smartsign.clean_captcha(buf.getvalue())))
    assert out.getpixel((1, 1)) == 255 and out.getpixel((2, 2)) == 0


@pytest.mark.parametrize("inv", [
    {"shdon": 50, "ma_tra_cuu": None, "tt_khac": {}},                     # HĐ 2023: không có TTKhac
    {"shdon": 50, "ma_tra_cuu": None, "tt_khac": {"Matracuu": "ab-c"}},
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        smartsign.SmartSignAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_lookup_code():
    a = smartsign.SmartSignAdapter
    assert a.lookup_code({"tt_khac": {"Matracuu": " 26K8049787RX "}}) == "26K8049787RX"
    assert a.lookup_code({"tt_khac": {"MaTraCuu": "X1"}, "ma_tra_cuu": "Y"}) == "X1"
    assert a.lookup_code({"tt_khac": {}, "ma_tra_cuu": "Y2"}) == "Y2"


def test_invoice_from_link():
    a = smartsign.SmartSignAdapter()
    assert a.invoice_from_link("https://ehd.smartsign.com.vn/", "26G33042866Y", "")["ma_tra_cuu"] == "26G33042866Y"
    assert a.invoice_from_link("https://tracuuhd.smartsign.com.vn/", None, "") is None


def test_declares_mst_tcgp():
    a = smartsign.SmartSignAdapter()
    assert a.source_id == "smartsign" and a.mst_tcgp == ("0309612872",) and a.hosts == ("smartsign.com.vn",)
