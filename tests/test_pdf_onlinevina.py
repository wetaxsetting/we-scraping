"""Adapter Online VI NA / hoadon.onlinevina.com.vn (HTTP + Chromium giả lập, không cần mạng).

Luồng thật đã kiểm 26/10/2026: 1/1 HĐ thật DIDY C24TDD-14 — API trả XML + XSLT, dựng PDF bằng
lxml + Chromium; chữ trùng 100% PDF trong mail.

Run::

    pytest tests/test_pdf_onlinevina.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import onlinevina       # noqa: E402
from pdf_providers import render           # noqa: E402

CODE = "6604fc6cfef036498fbc01ad"
XML = ("<TDiep><TTChung><MLTDiep>202</MLTDiep></TTChung><DLieu><HDon><DLHDon><TTChung>"
       "<KHHDon>C24TDD</KHHDon><SHDon>14</SHDon></TTChung></DLHDon><MCCQT>00ABC</MCCQT></HDon></DLieu></TDiep>")
XSLT = ('<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        '<xsl:template match="/"><html><head><script>loadData()</script></head><body>'
        '<div id="background" style="display:none;">nen</div>'
        '<div style="display: none" class="newInvoice">Ky hieu <xsl:value-of select="//KHHDon"/> '
        'So <xsl:value-of select="//SHDon"/></div></body></html></xsl:template></xsl:stylesheet>')


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


def _blank_pdf() -> bytes:
    import fitz
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes() + b"%" + b" " * 1200          # đủ dài để is_pdf nhận, nhưng không có chữ
    doc.close()
    return data


class _Resp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data, status

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


OK = _Resp({"code": 50000, "message": "Thành công",
            "data": {"xml_content": XML, "xslt_content": XSLT, "status": "1", "state_of_bill": "0"}})
NOT_FOUND = _Resp({"code": 51202, "message": "Không tồn tại ID tìm kiếm hoá đơn", "data": None})


def _setup(monkeypatch, calls, resp=OK, pdf=None, xslt_html=None):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        assert url == onlinevina.VIEW
        return resp
    monkeypatch.setattr(onlinevina, "request", fake)
    monkeypatch.setattr(onlinevina, "new_session", lambda: object())
    pages = []

    def fake_render(body, base):
        pages.append(body)
        return pdf if pdf is not None else _pdf_with("So 14")
    monkeypatch.setattr(render, "html_to_pdf", fake_render)
    if xslt_html is not None:                     # khỏi cần lxml cho các test luồng
        monkeypatch.setattr(onlinevina, "xslt_to_html", lambda xml, xslt: xslt_html)
    return pages


INV = {"shdon": 14, "khhdon": "C24TDD", "nbmst": "0314620766", "ma_tra_cuu": None,
       "tt_khac": {"SearchInvoice": CODE, "Discount": "0.000"}}
PAGE = '<html><body><div style="display: none" class="newInvoice">So 14</div></body></html>'


def test_ok(monkeypatch):
    calls = []
    pages = _setup(monkeypatch, calls, xslt_html=PAGE)
    assert onlinevina.OnlineVinaAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert calls[0][2]["json"] == {"search_invoice_id": CODE}
    assert "display" not in pages[0]                        # khối nội dung đã hiện ra


def test_fetch_xml_returns_tdiep(monkeypatch):
    _setup(monkeypatch, [], xslt_html=PAGE)
    xml = onlinevina.OnlineVinaAdapter().fetch_xml(INV)
    assert xml.startswith(b"<TDiep>") and b"<SHDon>14</SHDon>" in xml


@pytest.mark.parametrize("resp, kind", [
    (NOT_FOUND, "no_pdf"),
    (_Resp({"code": 50000, "data": {"xml_content": XML.replace(">14<", ">15<"), "xslt_content": XSLT}}),
     "bad_content"),
    (_Resp({"code": 40000, "message": "lỗi", "data": None}), "bad_content"),
    (_Resp(None), "bad_content"),
])
def test_view_errors(monkeypatch, resp, kind):
    _setup(monkeypatch, [], resp=resp, xslt_html=PAGE)
    with pytest.raises(PdfFetchError) as ei:
        onlinevina.OnlineVinaAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


@pytest.mark.parametrize("pdf", ["blank", "other_invoice"])
def test_render_errors(monkeypatch, pdf):
    pdf = _blank_pdf() if pdf == "blank" else _pdf_with("So 99999")
    _setup(monkeypatch, [], pdf=pdf, xslt_html=PAGE)
    with pytest.raises(PdfFetchError) as ei:
        onlinevina.OnlineVinaAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [
    {"shdon": 1, "tt_khac": {}},
    {"shdon": 1, "tt_khac": {"SearchInvoice": "khong-phai-hex"}},
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        onlinevina.OnlineVinaAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_printable_unhides_content_but_not_background():
    out = onlinevina.printable('<script>x()</script><div id="background" class="b" style="display:none;">nen</div>'
                               '<div style="display: none" class="newInvoice">A</div><div style="display:none">B</div>')
    assert "<script" not in out
    assert out.count("display") == 1 and 'id="background" class="b" style="display:none;"' in out


def test_xslt_to_html_with_lxml():
    pytest.importorskip("lxml")
    hdon = XML[XML.index("<HDon>"):XML.index("</HDon>") + 7]
    html = onlinevina.xslt_to_html(hdon, XSLT)
    assert "Ky hieu C24TDD" in html and "So 14" in html


def test_xslt_cannot_read_files(tmp_path):
    pytest.importorskip("lxml")
    secret = tmp_path / "secret.xml"
    secret.write_text("<s>bi mat</s>", encoding="utf-8")
    evil = ('<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
            f'<xsl:template match="/"><p><xsl:value-of select="document(\'{secret.as_uri()}\')"/></p>'
            '</xsl:template></xsl:stylesheet>')
    try:
        out = onlinevina.xslt_to_html("<HDon/>", evil)
    except PdfFetchError:
        return
    assert "bi mat" not in out


def test_invoice_from_link():
    a = onlinevina.OnlineVinaAdapter()
    assert a.invoice_from_link(f"https://hoadon.onlinevina.com.vn/invoice?id={CODE}", None, "")["tt_khac"] \
        == {"SearchInvoice": CODE}
    assert a.invoice_from_link("https://hoadon.onlinevina.com.vn/invoice", CODE, "")["ma_tra_cuu"] == CODE
    assert a.invoice_from_link("https://hoadon.onlinevina.com.vn/invoice", None, "") is None
    assert a.invoice_from_link("https://onlinevina.com.vn/", CODE, "") is None


def test_declares_mst_tcgp():
    a = onlinevina.OnlineVinaAdapter()
    assert a.source_id == "onlinevina" and a.mst_tcgp == ("0314185087",) \
        and a.hosts == ("hoadon.onlinevina.com.vn",)
