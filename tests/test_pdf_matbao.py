"""Adapter Mắt Bão / matbao.in (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 26/10/2026: 2/2 HĐ thật Raffles (C24TAB-1281/1282) chỉ từ dữ liệu XML Thuế
(hỏi Fkey qua /fkey rồi tải PDF + XML); thêm 1 HĐ qua mã trong mail (2C25MQC-262).

Run::

    pytest tests/test_pdf_matbao.py -q
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
from pdf_providers import matbao           # noqa: E402

PDF_URL = "https://portal-group1.mifi.vn/downloadpdfca.aspx?keyinv=K&p=1&s=C24TAB"
XML_URL = "https://portal-group1.mifi.vn/downloadxml.aspx?idInvoice=K&pattern=1"


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
    def __init__(self, data=None, status=200, content=b"", headers=None):
        self._data, self.status_code, self.content = data, status, content
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


FOUND = _Resp({"found": True, "symbol": "1C24TAB", "number": "1282", "viewerUrl": "https://x.matbao.in/v",
               "pdfUrl": PDF_URL, "xmlUrl": XML_URL})


def _setup(monkeypatch, calls, fkey=_Resp({"fkey": "2EF6276D0B"}), lookup=FOUND, pdf=None, xml=None):
    pdf = pdf if pdf is not None else _Resp(content=_pdf_with("So 1282"), headers={"Content-Type": "application/pdf"})
    xml = xml if xml is not None else _Resp(content=b"<HDon><SHDon>1282</SHDon></HDon>")

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url == matbao.API + "/fkey":
            return fkey
        if url == matbao.API:
            return lookup
        if url == PDF_URL:
            return pdf
        if url == XML_URL:
            return xml
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(matbao, "request", fake)
    monkeypatch.setattr(matbao, "new_session", lambda: object())


INV = {"shdon": 1282, "khhdon": "C24TAB", "khmshdon": 1, "nbmst": "3502306822-001",
       "mst_tcgp": "0302712571", "ma_tra_cuu": None, "tt_khac": {}}


def test_ok_from_xml_fields(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    assert matbao.MatBaoAdapter().fetch_pdf(INV).startswith(b"%PDF")
    fk = [kw for _, u, kw in calls if u.endswith("/fkey")][0]
    assert fk["params"] == {"taxcode": "3502306822-001", "pattern": "1", "serial": "C24TAB", "no": "1282"}
    lk = [kw for _, u, kw in calls if u == matbao.API][0]
    assert lk["params"] == {"code": "2EF6276D0B", "keyword": ""} and "token" not in lk["params"]


def test_uses_code_when_known(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    matbao.MatBaoAdapter().fetch_pdf(dict(INV, ma_tra_cuu="2EF6276D0B"))
    assert not any(u.endswith("/fkey") for _, u, _ in calls)


def test_fetch_xml(monkeypatch):
    _setup(monkeypatch, [])
    assert b"<SHDon>1282</SHDon>" in matbao.MatBaoAdapter().fetch_xml(INV)


@pytest.mark.parametrize("fkey, lookup, kind", [
    (_Resp({"fkey": None}), FOUND, "no_pdf"),
    (_Resp({"fkey": "2EF6276D0B"}), _Resp({"found": False}), "no_pdf"),
    (_Resp({"fkey": "2EF6276D0B"}), _Resp({"found": True, "number": "1283", "pdfUrl": PDF_URL}), "bad_content"),
    (_Resp({"fkey": "2EF6276D0B"}), _Resp({"found": True, "number": "1282"}), "no_pdf"),        # thiếu pdfUrl
    (_Resp(status=403), FOUND, "no_portal"),                  # server bật reCAPTCHA thật → không vượt
    (_Resp({"fkey": "2EF6276D0B"}), _Resp(status=403), "no_portal"),
    (_Resp({"fkey": "2EF6276D0B"}), _Resp(status=404), "bad_content"),
])
def test_lookup_errors(monkeypatch, fkey, lookup, kind):
    _setup(monkeypatch, [], fkey=fkey, lookup=lookup)
    with pytest.raises(PdfFetchError) as ei:
        matbao.MatBaoAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


@pytest.mark.parametrize("pdf", [
    _Resp(content=b"<html>" + b"x" * 2000, headers={"Content-Type": "text/html"}),
    "other_invoice",
    _Resp(status=404),
])
def test_download_errors(monkeypatch, pdf):
    if pdf == "other_invoice":
        pdf = _Resp(content=_pdf_with("So 99999"))
    _setup(monkeypatch, [], pdf=pdf)
    with pytest.raises(PdfFetchError) as ei:
        matbao.MatBaoAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


def test_xml_not_xml(monkeypatch):
    _setup(monkeypatch, [], xml=_Resp(content=b"<html>loi</html>"))
    with pytest.raises(PdfFetchError) as ei:
        matbao.MatBaoAdapter().fetch_xml(INV)
    assert ei.value.kind == "bad_content"


def test_no_code_without_xml_fields():
    with pytest.raises(PdfFetchError) as ei:
        matbao.MatBaoAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = matbao.MatBaoAdapter()
    for url in ("https://matbao.in", "https://portal.matbao.in/Invoice/ViewFromEmail?token=cm9t&tk=1",
                "https://portal.matbao.in/HoaDonDauVao/KiemTraHopLe/dG1Y/U0Rw"):
        assert a.invoice_from_link(url, "Y487A22F9F", "")["ma_tra_cuu"] == "Y487A22F9F"
    assert a.invoice_from_link("https://matbao.in/tra-cuu-hoa-don?ma-tra-cuu=AB12CD34EF", None, "")["tt_khac"] \
        == {"Fkey": "AB12CD34EF"}
    assert a.invoice_from_link("https://matbao.in", None, "") is None
    assert a.invoice_from_link("https://example.com", "Y487A22F9F", "") is None


def test_declares_mst_tcgp():
    a = matbao.MatBaoAdapter()
    assert a.source_id == "matbao" and a.mst_tcgp == ("0302712571",) and a.hosts == ("matbao.in",)
