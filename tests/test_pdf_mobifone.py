"""Adapter MobiFone (tracuuhoadonvienthong / tracuuhoadon .mobifone.vn), HTTP giả lập —
luồng thật đã kiểm 27/10/2026 (10/10 mã trong mail).

Run::

    pytest tests/test_pdf_mobifone.py -q
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common, mobifone  # noqa: E402

TELECOM = "https://tracuuhoadonvienthong.mobifone.vn"
STORE = "https://tracuuhoadon.mobifone.vn"


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


def _zip_with(xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("invoice.xml", xml)
        z.writestr("invoice__1K23DAD_1213289.pdf", b"%PDF-1.7 x")
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, content=b"", js=None, ctype="application/pdf"):
        self.status_code, self.content, self._js = status, content, js
        self.headers = {"Content-Type": ctype}
        self.text = content.decode("utf-8", "replace")

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        res = self.results.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(mobifone, "new_session", lambda: sess)
    return sess


def _info(**row) -> _Resp:
    return _Resp(js={"data": [row]}, ctype="application/json")


NOT_FOUND = _Resp(js={"error": "Không tồn tại hóa đơn có số bảo mật: X"}, ctype="application/json")
INV = {"shdon": "1213289", "nbmst": "0100686209", "ma_tra_cuu": "EqW7wm", "tt_khac": {},
       "lookup_url": "http://tracuuhoadonvienthong.mobifone.vn/Tracuu/PrintInvoicePdf?sobaomat=EqW7wm"}
ROW_TT32 = {"security_number": "EqW7wm", "invoice_number": "1213289", "invoice_series": "1K23DAD",
            "mst": "0100686209"}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0100686209").source_id == "mobifone"
    assert pdf_providers.adapter_for_mst_tcgp("0100686209-002").source_id == "mobifone"
    for h in ("tracuuhoadonvienthong.mobifone.vn", "tracuuhoadon.mobifone.vn",
              "tracuuhoadon.mobifoneinvoice.vn"):
        assert pdf_providers.adapter_for_host(h).source_id == "mobifone"


def test_ok_telecom(monkeypatch):
    sess = _use(monkeypatch, _info(**ROW_TT32), _Resp(content=_pdf_with("So 1213289")))
    assert mobifone.MobifoneAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    (m1, u1, kw1), (m2, u2, kw2) = sess.calls
    assert m1 == "POST" and u1 == f"{TELECOM}/Tracuu2/GetInfoInvoice"
    assert kw1["json"]["sobaomat"] == "EqW7wm"
    assert u2 == f"{TELECOM}/Tracuu2/PrintInvoice"
    assert kw2["json"] == {"sobaomat": "EqW7wm", "masothue": "0100686209", "type": "PDF",
                           "typeSearch": "hoadon"}


def test_unknown_portal_falls_back_to_store(monkeypatch):
    """Mã không rõ cổng: thử cổng viễn thông, "không tồn tại" → cổng cửa hàng (dạng TT78: sbmat/shdon)."""
    sess = _use(monkeypatch, NOT_FOUND,
                _info(sbmat="7514307", shdon="1216", khieu="1K25TVB", mst="0100686209"),
                _Resp(content=_pdf_with("So 1216")))
    inv = {"shdon": 1216, "nbmst": "0100686209-002", "ma_tra_cuu": "7514307", "tt_khac": {}}
    assert mobifone.MobifoneAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert [c[1] for c in sess.calls] == [f"{TELECOM}/Tracuu2/GetInfoInvoice",
                                          f"{STORE}/Tracuu2/GetInfoInvoice",
                                          f"{STORE}/Tracuu2/PrintInvoice"]
    assert sess.calls[2][2]["json"]["sobaomat"] == "7514307"


def test_partner_seller_uses_mobifoneinvoice(monkeypatch):
    sess = _use(monkeypatch, _info(security_number="ABC123", invoice_number="5", mst="0312345678"),
                _Resp(content=_pdf_with("So 5")))
    inv = {"shdon": 5, "nbmst": "0312345678", "mst_tcgp": "0100686209",
           "tt_khac": {"Số bảo mật": "ABC123"}}
    mobifone.MobifoneAdapter().fetch_pdf(inv)
    assert sess.calls[0][1] == "http://tracuuhoadon.mobifoneinvoice.vn/Tracuu2/GetInfoInvoice"
    assert sess.calls[0][2]["json"]["masothue"] == "0312345678"


def test_not_found_everywhere(monkeypatch):
    _use(monkeypatch, NOT_FOUND, NOT_FOUND)
    with pytest.raises(PdfFetchError) as ei:
        mobifone.MobifoneAdapter().fetch_pdf({"shdon": 1, "ma_tra_cuu": "ZZZZ99", "tt_khac": {}})
    assert ei.value.kind == "no_pdf"


def test_wrong_invoice_number(monkeypatch):
    _use(monkeypatch, _info(**{**ROW_TT32, "invoice_number": "999"}))
    with pytest.raises(PdfFetchError) as ei:
        mobifone.MobifoneAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [
    {"shdon": 1, "tt_khac": {}},                                  # HĐ từ XML Thuế: không có số bảo mật
    {"shdon": 1, "ma_tra_cuu": "có dấu cách", "tt_khac": {}},
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        mobifone.MobifoneAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


@pytest.mark.parametrize("result, kind", [
    (_Resp(400, "Không tồn tại hóa đơn có số bảo mật EqW7wm".encode(), None, "text/plain"), "no_pdf"),
    (_Resp(500, b"loi", None, "text/html"), "server"),
    (_Resp(429, b""), "rate_limit"),
    (requests.Timeout("t"), "network"),
    (_Resp(200, b"<html>" + b"x" * 2000, None, "text/html"), "bad_content"),
    (_Resp(200, _pdf_with("So 99999")), "bad_content"),      # PDF của HĐ khác
])
def test_print_errors(monkeypatch, result, kind):
    _use(monkeypatch, _info(**ROW_TT32), result)
    with pytest.raises(PdfFetchError) as ei:
        mobifone.MobifoneAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


def test_info_server_error(monkeypatch):
    _use(monkeypatch, _Resp(js={"error": "42P01: relation does not exist"}, ctype="application/json"))
    with pytest.raises(PdfFetchError) as ei:
        mobifone.MobifoneAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "server"


def test_fetch_xml(monkeypatch):
    xml = "<HDon><DLHDon><TTChung><SHDon>1213289</SHDon></TTChung></DLHDon></HDon>".encode()
    sess = _use(monkeypatch, _info(**ROW_TT32), _Resp(content=_zip_with(xml), ctype="application/zip"))
    assert mobifone.MobifoneAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[1][1] == f"{TELECOM}/Tracuu2/ExportZipFileXML"
    _use(monkeypatch, _info(**ROW_TT32),
         _Resp(content=_zip_with(xml.replace(b"1213289", b"7")), ctype="application/zip"))
    with pytest.raises(PdfFetchError) as ei:
        mobifone.MobifoneAdapter().fetch_xml(dict(INV))
    assert ei.value.kind == "bad_content"


def test_invoice_from_link():
    a = mobifone.MobifoneAdapter()
    inv = a.invoice_from_link(
        "http://tracuuhoadonvienthong.mobifone.vn/Tracuu/PrintInvoicePdf?sobaomat=EqW7wm", "EqW7wm",
        "FW: Thông báo xuất hóa đơn điện tử  1213289")
    assert inv["ma_tra_cuu"] == "EqW7wm" and inv["shdon"] == "1213289"
    assert inv["mst_tcgp"] == "0100686209"
    assert mobifone.MobifoneAdapter.portals(inv) == [(TELECOM, "0100686209")]
    inv = a.invoice_from_link("https://tracuuhoadon.mobifone.vn", "7514307",
                              "Cửa Hàng Trần Não (MobiFone Thủ Đức) thông báo phát hành hóa đơn "
                              "điện tử số 1216 của Công Ty TNHH Webcash Việt Nam")
    assert inv["ma_tra_cuu"] == "7514307" and inv["shdon"] == "1216"
    assert mobifone.MobifoneAdapter.portals(inv) == [(STORE, "0100686209")]
    assert a.invoice_from_link("https://tracuuhoadon.mobifone.vn", None, "") is None
    assert a.invoice_from_link("https://example.com/x?sobaomat=EqW7wm", None, "") is None
