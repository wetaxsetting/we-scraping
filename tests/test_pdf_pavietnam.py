"""Adapter P.A Việt Nam / hoadon.pavietnam.vn (HTTP + ddddocr giả lập, không cần mạng).

Luồng thật đã kiểm 10/2026: 7/7 HĐ thật tải được PDF khi có mã tra cứu (mã lấy từ PDF trong
mail — XML không có mã); ddddocr sau khi lọc chữ đỏ được server nhận 40/40 captcha.

Run::

    pytest tests/test_pdf_pavietnam.py -q
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
from pdf_providers import pavietnam        # noqa: E402

B = pavietnam.BASE
PDF_URL = "https://bill.hoadon30s.vn/lookup/5bb930936a96a8649432327259855a4536f8bb01/pdf-download"


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


def _toast(msg):
    return _Resp(f"<script>toastr.error('{msg}');</script>")


def _rows(*rows):
    return _Resp("<table>" + "".join(
        f'<tr><td data-label="Số hóa đơn" style="word-break: break-word;">{no} </td>'
        f'<td data-label="Thao tác"><a href="{url.replace("/pdf-download", "/view")}">v</a>'
        f'<a href="{url}" class="viewmore" title="Download PDF">p</a></td></tr>' for no, url in rows) + "</table>")


HOME = _Resp('<input name="token" id="token" type="hidden" value="tk1" />')
WRONG = _toast("Mã bảo vệ không chính xác")
NOT_FOUND = _toast("Không tìm thấy kết quả hóa đơn. Vui lòng kiểm tra lại mã tra cứu")
FOUND = _rows(("00088744", PDF_URL))


def _setup(monkeypatch, calls, lookup, pdf=None, answers=("A3BAD1",), home=HOME):
    lookup, answers = list(lookup), list(answers)
    pdf = pdf if pdf is not None else _Resp(content=_pdf_with("So 88744"), headers={"Content-Type": "application/pdf"})

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url == B:
            return home
        if url == B + "captcha.php":
            return _Resp(content=b"png")
        if url == B + "lookup.php":
            return lookup.pop(0)
        if url.startswith("https://bill.hoadon30s.vn/"):
            return pdf
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(pavietnam, "request", fake)
    monkeypatch.setattr(pavietnam, "new_plain_session", lambda: object())
    monkeypatch.setattr(pavietnam, "clean_captcha", lambda img: img)
    monkeypatch.setattr(pavietnam, "ocr_text", lambda img: answers.pop(0) if len(answers) > 1 else answers[0])


INV = {"shdon": 88744, "khhdon": "C26TPA", "khmshdon": 1, "nbmst": "0302431595",
       "ma_tra_cuu": "BLJDKOIDNZOYIP", "tt_khac": {"SDHang": ""}}


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [FOUND])
    assert pavietnam.PaVietnamAdapter().fetch_pdf(INV).startswith(b"%PDF")
    post = [kw for _, u, kw in calls if u.endswith("lookup.php")][0]["data"]
    assert post["OrderID"] == "BLJDKOIDNZOYIP" and post["SecurityCode"] == "A3BAD1"
    assert post["token"] == "tk1" and post["ckbtype"] == "text" and post["Username"] == ""
    assert any(u == PDF_URL for _, u, _ in calls)


def test_accepts_db_shaped_invoice(monkeypatch):
    inv = dict(INV, tong_tien=Decimal("108000"), ngay_lap=date(2026, 6, 11))
    _setup(monkeypatch, [], [FOUND])
    assert pavietnam.PaVietnamAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_picks_row_matching_shdon(monkeypatch):
    other = PDF_URL.replace("5bb9", "0000")
    calls = []
    _setup(monkeypatch, calls, [_rows(("00011708", other), ("00088744", PDF_URL))])
    pavietnam.PaVietnamAdapter().fetch_pdf(INV)
    assert any(u == PDF_URL for _, u, _ in calls) and not any(u == other for _, u, _ in calls)


def test_code_points_to_other_invoice(monkeypatch):
    _setup(monkeypatch, [], [_rows(("00011708", PDF_URL))])
    with pytest.raises(PdfFetchError) as ei:
        pavietnam.PaVietnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_retries_wrong_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [WRONG, FOUND])
    assert pavietnam.PaVietnamAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert sum(u.endswith("captcha.php") for _, u, _ in calls) == 2


def test_gives_up_after_tries(monkeypatch):
    _setup(monkeypatch, [], [WRONG] * pavietnam.CAPTCHA_TRIES)
    with pytest.raises(PdfFetchError) as ei:
        pavietnam.PaVietnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"


def test_does_not_send_bad_answer(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, [FOUND], answers=("A3BA", "a3bad1"))
    pavietnam.PaVietnamAdapter().fetch_pdf(INV)
    posts = [kw["data"] for _, u, kw in calls if u.endswith("lookup.php")]
    assert len(posts) == 1 and posts[0]["SecurityCode"] == "A3BAD1"      # hex HOA


def test_solve_maps_lookalikes(monkeypatch):
    monkeypatch.setattr(pavietnam, "clean_captcha", lambda img: img)
    monkeypatch.setattr(pavietnam, "ocr_text", lambda img: "4f3O2e")
    assert pavietnam.solve(b"x") == "4F302E"


def test_not_found(monkeypatch):
    _setup(monkeypatch, [], [NOT_FOUND])
    with pytest.raises(PdfFetchError) as ei:
        pavietnam.PaVietnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_waf_block_is_temporary(monkeypatch):
    _setup(monkeypatch, [], [], home=_Resp("<title>safeline</title>", status=403))
    with pytest.raises(PdfFetchError) as ei:
        pavietnam.PaVietnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == "server"


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
        pavietnam.PaVietnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code_when_xml_only():
    """Đúng ca thật: XML của P.A không có mã tra cứu."""
    with pytest.raises(PdfFetchError) as ei:
        pavietnam.PaVietnamAdapter().fetch_pdf(dict(INV, ma_tra_cuu=None))
    assert ei.value.kind == "no_code"


def test_other_seller_has_no_portal():
    with pytest.raises(PdfFetchError) as ei:
        pavietnam.PaVietnamAdapter().fetch_pdf(dict(INV, nbmst="0311111111"))
    assert ei.value.kind == "no_portal"


def test_clean_captcha_keeps_red_text():
    from PIL import Image
    im = Image.new("RGB", (12, 6), (200, 200, 200))
    for x in range(3, 9):
        im.putpixel((x, 3), (255, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out = Image.open(io.BytesIO(pavietnam.clean_captcha(buf.getvalue()))).convert("L")
    assert out.size == (12, 6)
    assert out.getpixel((5, 3)) < 128 and out.getpixel((0, 0)) > 200


def test_invoice_from_link():
    a = pavietnam.PaVietnamAdapter()
    assert a.invoice_from_link("https://hoadon.pavietnam.vn", "BLJDKOIDNZOYIP", "")["ma_tra_cuu"] == "BLJDKOIDNZOYIP"
    assert a.invoice_from_link("https://hoadon.pavietnam.vn", None, "") is None


def test_declares_mst_tcgp():
    a = pavietnam.PaVietnamAdapter()
    assert a.source_id == "pavietnam" and a.mst_tcgp == ("0302431595",) and a.hosts == ("hoadon.pavietnam.vn",)
