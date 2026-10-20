"""Adapter iPOS (tracuuhoadon.ipos.vn), HTTP giả lập — luồng thật đã kiểm 10/2026 trên HĐ MTT.

Dữ liệu trong test là GIẢ (MST, số HĐ, id, mã bí mật).

Run::

    pytest tests/test_pdf_ipos.py -q
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import ipos             # noqa: E402

SELLER = "0100000001"
TOKEN = base64.b64encode(f"1:C26MAA:123:{SELLER}".encode()).decode()
INV = {"nbmst": SELLER, "khmshdon": 1, "khhdon": "C26MAA", "shdon": 123, "tt_khac": {}}
BASE = "https://tracuuhoadon.ipos.vn"

MAIL = f"""Thông báo Về việc xuất Hoá đơn điện tử
<p>* Ký hiệu hóa đơn: <strong>1C26MAA</strong></p><p>* Số hóa đơn: <strong>00000123</strong></p>
<a href="{BASE}/Invoice/getinvoice?token={TOKEN}">(Tại đây)</a>
<a href="{BASE}/Invoice/Download78?id=555">(Tại đây)</a>
<a href="{BASE}/Invoice/ViewFromEmail?token={TOKEN}">(Tại đây)</a> theo mã số bí mật <b>FAKESECRET1</b>
Ghi chú: Thông báo này được gửi tự động từ hệ thống Hóa Đơn Điện Tử của CÔNG TY GIẢ – MST: {SELLER}"""

XML = (f"<HDon><DLHDon Id=\"DL\"><TTChung><KHMSHDon>1</KHMSHDon><KHHDon>C26MAA</KHHDon>"
       f"<SHDon>00000123</SHDon></TTChung><NDHDon><NBan><MST>{SELLER}</MST></NBan></NDHDon>"
       f"</DLHDon><DSCKS><NBan /></DSCKS></HDon>").encode()


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
    def __init__(self, status=200, content=b"", text="", headers=None):
        self.status_code, self.content, self.text = status, content, text
        self.headers = headers or {}


NOT_FOUND = _Resp(302, b"<html>Object moved</html>", headers={"Location": "/Invoice/Index"})


def _router(monkeypatch, handler, calls):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        return handler(url, kw.get("params") or {})
    monkeypatch.setattr(ipos, "request", fake)
    monkeypatch.setattr(ipos, "new_session", lambda: object())


# -- invoice_from_link ---------------------------------------------------------

@pytest.mark.parametrize("path", ["/Invoice/getinvoice", "/Invoice/ViewFromEmail"])
def test_link_with_token(path):
    inv = ipos.IposAdapter().invoice_from_link(f"{BASE}{path}?token={TOKEN}", "FAKESECRET1", MAIL)
    assert {k: inv[k] for k in ("nbmst", "khmshdon", "khhdon", "shdon", "ipos_token", "ipos_id",
                                "ma_tra_cuu", "mst_tcgp", "loai_hd")} == {
        "nbmst": SELLER, "khmshdon": 1, "khhdon": "C26MAA", "shdon": 123, "ipos_token": TOKEN,
        "ipos_id": "555", "ma_tra_cuu": "FAKESECRET1", "mst_tcgp": "0104789847", "loai_hd": "MTT"}


def test_download78_link_takes_token_from_same_mail():
    inv = ipos.IposAdapter().invoice_from_link(f"{BASE}/Invoice/Download78?id=555", "555", MAIL)
    assert (inv["shdon"], inv["ipos_id"], inv["ipos_token"], inv["ma_tra_cuu"]) == (123, "555", TOKEN, None)


def test_download78_link_falls_back_to_mail_fields():
    text = MAIL.replace(f"getinvoice?token={TOKEN}", "x").replace(f"ViewFromEmail?token={TOKEN}", "y")
    inv = ipos.IposAdapter().invoice_from_link(f"{BASE}/Invoice/Download78?id=555", None, text)
    assert (inv["khmshdon"], inv["khhdon"], inv["shdon"], inv["nbmst"]) == (1, "C26MAA", 123, SELLER)
    assert inv["ipos_token"] == TOKEN


@pytest.mark.parametrize("url, text", [
    (f"{BASE}/", MAIL),                                             # trang chủ
    (f"{BASE}/Invoice/Search", MAIL),
    (f"{BASE}/Content/images/logonew.jpg", MAIL),                   # ảnh
    (f"{BASE}/Invoice/getinvoice?token=not-base64!!", ""),          # token hỏng, mail không có gì
    (f"{BASE}/Invoice/Download78?id=555", ""),                      # chỉ có id, không biết HĐ
    (f"https://tracuuhoadon.fpt.com.vn/?mst={SELLER}&sec=abc", MAIL),  # host khác
    ("https://evil-ipos.vn.example.com/Invoice/getinvoice?token=" + TOKEN, MAIL),
])
def test_unusable_links(url, text):
    assert ipos.IposAdapter().invoice_from_link(url, None, text) is None


# -- fetch_pdf -------------------------------------------------------------------

@pytest.mark.parametrize("inv", [
    INV,
    {**INV, "khmshdon": "1", "shdon": "00000123"},                  # dạng chuỗi từ XML
    {**INV, "khhdon": "1C26MAA"},                                   # mẫu số dính ký hiệu
])
def test_pdf_ok_from_invoice_fields(monkeypatch, inv):
    calls = []
    pdf = _pdf_with("So 00000123")
    _router(monkeypatch, lambda url, p: _Resp(content=pdf, headers={"Content-Type": "application/pdf"}),
            calls)
    assert ipos.IposAdapter().fetch_pdf(inv) == pdf
    (method, url, kw), = calls
    assert (method, url, kw["params"], kw["allow_redirects"]) == (
        "GET", f"{BASE}/Invoice/getinvoice", {"token": TOKEN}, False)


def test_pdf_not_found(monkeypatch):
    _router(monkeypatch, lambda url, p: NOT_FOUND, [])
    with pytest.raises(PdfFetchError) as ei:
        ipos.IposAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


@pytest.mark.parametrize("resp, kind", [
    (_Resp(content=b"<html>" + b"x" * 2000, headers={"Content-Type": "text/html"}), "bad_content"),
    (_Resp(content=_pdf_with("HD 99999")), "bad_content"),         # PDF của HĐ khác
    (_Resp(status=404), "other"),
])
def test_pdf_errors(monkeypatch, resp, kind):
    _router(monkeypatch, lambda url, p: resp, [])
    with pytest.raises(PdfFetchError) as ei:
        ipos.IposAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_pdf_without_fields():
    with pytest.raises(PdfFetchError) as ei:
        ipos.IposAdapter().fetch_pdf({"shdon": 1, "khhdon": "", "nbmst": ""})
    assert ei.value.kind == "no_code"


# -- fetch_xml -------------------------------------------------------------------

def test_xml_via_view_page(monkeypatch):
    calls = []

    def handler(url, params):
        if url.endswith("/ViewFromEmail"):
            return _Resp(text=f"<script>ajxCall4Portal('555', '1','C26MAA','{SELLER}', false, false);</script>")
        return _Resp(content=XML)
    _router(monkeypatch, handler, calls)
    assert ipos.IposAdapter().fetch_xml(INV) == XML
    assert [(u.rsplit("/", 1)[1], kw["params"]) for _, u, kw in calls] == [
        ("ViewFromEmail", {"token": TOKEN}), ("Download78", {"id": "555"})]


def test_xml_uses_id_from_mail(monkeypatch):
    calls = []
    _router(monkeypatch, lambda url, p: _Resp(content=XML), calls)
    assert ipos.IposAdapter().fetch_xml({**INV, "ipos_id": "555"}) == XML
    assert len(calls) == 1


def test_xml_not_found(monkeypatch):
    _router(monkeypatch, lambda url, p: NOT_FOUND, [])
    with pytest.raises(PdfFetchError) as ei:
        ipos.IposAdapter().fetch_xml(INV)
    assert ei.value.kind == "no_pdf"


def test_xml_of_another_invoice(monkeypatch):
    _router(monkeypatch, lambda url, p: _Resp(content=XML.replace(b"00000123", b"00000999")), [])
    with pytest.raises(PdfFetchError) as ei:
        ipos.IposAdapter().fetch_xml({**INV, "ipos_id": "555"})
    assert ei.value.kind == "bad_content"


def test_registered():
    a = pdf_providers.get_adapter("ipos")
    assert a is not None and a.mst_tcgp == ("0104789847",) and a.hosts == ("ipos.vn",)
