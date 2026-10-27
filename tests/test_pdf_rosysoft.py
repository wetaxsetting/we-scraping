"""Adapter ROSY / einv.rosysoft.vn:8386 (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 26/10/2026: 24 HĐ từ link mail Ánh Dương (Vinasun) + 2 HĐ trong DB tra theo ký hiệu/số
→ 26/26 tải được PDF + XML có chữ ký (25 HĐ người mua 1201496252, 1 HĐ của người mua khác).

Run::

    pytest tests/test_pdf_rosysoft.py -q
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
from pdf_providers import rosysoft         # noqa: E402

BASE = rosysoft.BASE


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
        self.text = content.decode("utf-8", "replace")
        self.headers = {}

    def json(self):
        if self._json is None:
            raise ValueError("không phải JSON")
        return self._json


ROW = {"InvID": "A01TXL000665500", "InvNo": "00025121", "InvSerial": "1K24TAN", "TaxCode": "0302035520",
       "CusTaxCode": "1201496252", "PINCode": "9744"}


def _setup(monkeypatch, calls, rows=(ROW,), pdf=None, xml=b"<HDon><SHDon>25121</SHDon></HDon>"):
    pdf = pdf if pdf is not None else _pdf_with("So 00025121")

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET" and url == BASE + "/RSeInvoiceSearch":
            return _Resp()
        if url == BASE + "/RSeInvoiceSearch/Read":
            return _Resp({"Data": list(rows), "Total": len(rows)})
        if url == BASE + "/RSeInvoiceDetail/CallApi_GetPDF_HDDT":
            return _Resp({"success": True, "message": "OK", "PDF_File": base64.b64encode(pdf).decode()})
        if url == BASE + "/RSeInvoiceDetail/viewXml":
            return _Resp(content=xml)
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(rosysoft, "request", fake)
    monkeypatch.setattr(rosysoft, "new_session", lambda: object())


def test_link_flow(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    a = rosysoft.RosySoftAdapter()
    inv = a.invoice_from_link(f"{BASE}/RSeInvoiceSearch/index?TaxCode=0302035520&InvID=A01TXL000665500",
                              "A01TXL000665500", "1K24TAN 1 | E-Invoice  CÔNG TY CỔ PHẦN ÁNH DƯƠNG VIỆT NAM")
    assert inv["nbmst"] == "0302035520" and inv["ma_tra_cuu"] == "A01TXL000665500"
    assert a.fetch_pdf(dict(inv)).startswith(b"%PDF")
    assert calls[0][2]["params"] == {"TaxCode": "0302035520", "InvID": "A01TXL000665500"}
    get_pdf = [kw for _, u, kw in calls if u.endswith("CallApi_GetPDF_HDDT")][0]
    assert get_pdf["data"] == {"id": "A01TXL000665500", "taxcode": "0302035520", "PINcode": "9744"}


def test_search_by_serial_and_number(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    inv = {"nbmst": "0302035520", "khmshdon": 1, "khhdon": "K24TAN", "shdon": 25121}
    assert rosysoft.RosySoftAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert calls[0][2]["params"] == {"TaxCode": "0302035520", "InvSerial": "1K24TAN", "InvNo": "25121"}


def test_fetch_xml(monkeypatch):
    _setup(monkeypatch, [])
    inv = {"nbmst": "0302035520", "khmshdon": 1, "khhdon": "K24TAN", "shdon": 25121}
    assert b"<SHDon>25121</SHDon>" in rosysoft.RosySoftAdapter().fetch_xml(inv)


def test_xml_other_number(monkeypatch):
    _setup(monkeypatch, [], xml=b"<HDon><SHDon>99</SHDon></HDon>")
    with pytest.raises(PdfFetchError) as ei:
        rosysoft.RosySoftAdapter().fetch_xml({"nbmst": "0302035520", "ma_tra_cuu": "A01TXL000665500"})
    assert ei.value.kind == "bad_content"


def test_not_found(monkeypatch):
    _setup(monkeypatch, [], rows=())
    with pytest.raises(PdfFetchError) as ei:
        rosysoft.RosySoftAdapter().fetch_pdf({"nbmst": "0302035520", "ma_tra_cuu": "A01TXL000000000"})
    assert ei.value.kind == "no_pdf"


def test_row_number_mismatch_filtered(monkeypatch):
    _setup(monkeypatch, [], rows=(dict(ROW, InvNo="00000007"),))
    with pytest.raises(PdfFetchError) as ei:
        rosysoft.RosySoftAdapter().fetch_pdf({"nbmst": "0302035520", "khhdon": "K24TAN", "shdon": 25121})
    assert ei.value.kind == "no_pdf"


def test_pdf_of_other_invoice(monkeypatch):
    _setup(monkeypatch, [], pdf=_pdf_with("So 00099999"))
    with pytest.raises(PdfFetchError) as ei:
        rosysoft.RosySoftAdapter().fetch_pdf({"nbmst": "0302035520", "khhdon": "K24TAN", "shdon": 25121})
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [{"ma_tra_cuu": "A01TXL000665500"}, {"nbmst": "0302035520"}])
def test_missing_inputs(inv):
    with pytest.raises(PdfFetchError) as ei:
        rosysoft.RosySoftAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_invoice_from_link_variants():
    a = rosysoft.RosySoftAdapter()
    inv = a.invoice_from_link(f"{BASE}/RSeInvoiceDetail/ViewHtml?id=A01TXL000588415&taxcode=0302035520&PINcode=9518",
                              "A01TXL000588415", "")
    assert inv["nbmst"] == "0302035520" and inv["tt_khac"] == {"InvID": "A01TXL000588415"}
    assert a.invoice_from_link(f"{BASE}/", "A01TXL000588415", "") is None          # trang chủ: thiếu MST
    assert a.invoice_from_link("https://other.vn/?TaxCode=0302035520&InvID=A01", "x", "") is None


def test_declares():
    a = rosysoft.RosySoftAdapter()
    assert a.source_id == "rosysoft" and a.mst_tcgp == ("0305142231",) and a.hosts == ("rosysoft.vn",)
