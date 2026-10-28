"""Adapter Bách Khoa (hdbk.pmbk.vn / hoadonbachkhoa.pmbk.vn), HTTP giả lập — chưa có HĐ thật (27/10/2026).

Run::

    pytest tests/test_pdf_pmbk.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                   # noqa: E402
from pdf_providers import PdfFetchError                # noqa: E402
from pdf_providers import common, pmbk                 # noqa: E402


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
    def __init__(self, status=200, content=b"", js=None, ctype="application/pdf"):
        self.status_code, self.content, self._js = status, content, js
        self.headers = {"Content-Type": ctype}

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


def _J(js, status=200):
    return _Resp(status, b"{...}", js, "application/json")


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
    monkeypatch.setattr(pmbk, "new_session", lambda: sess)
    return sess


EMPTY = _Resp(204, b"", None, "")
NEW_API = "https://hdbk.pmbk.vn/api/HoaDonDienTu"
INV = {"shdon": 57, "khhdon": "C25TBK", "mst_tcgp": "0202029650", "ma_tra_cuu": None,
       "tt_khac": {"Mã tra cứu": "a1b2c3d4e5f6"}}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0202029650").source_id == "pmbk"
    assert pdf_providers.adapter_for_mst_tcgp("0200784873").source_id == "pmbk"
    assert pdf_providers.adapter_for_host("hdbk.pmbk.vn").source_id == "pmbk"
    assert pdf_providers.adapter_for_host("hoadonbachkhoa.pmbk.vn").source_id == "pmbk"


@pytest.mark.parametrize("inv, codes", [
    (INV, ["a1b2c3d4e5f6"]),
    ({"ma_tra_cuu": "XYZ123456", "tt_khac": {"MTCuu": "XYZ123456", "Mã số bí mật": "nope12345"}},
     ["XYZ123456"]),
    ({"tt_khac": {"LookupCode": "abc-123-def"}}, ["abc-123-def"]),
    ({"tt_khac": {"Mã tra cứu": "có dấu cách"}}, []),
    ({"tt_khac": {}}, []),
])
def test_lookup_codes(inv, codes):
    assert pmbk.PmbkAdapter.lookup_codes(inv) == codes


def test_ok_new_portal(monkeypatch):
    sess = _use(monkeypatch, _J({"path": "Uploaded/db1/pdf/hd57.pdf", "data": {"hoaDonDienTuId": "g1"}}),
                _Resp(content=_pdf_with("So 57")))
    assert pmbk.PmbkAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    assert sess.calls[0][:2] == ("GET", f"{NEW_API}/TraCuuByMa/a1b2c3d4e5f6")
    assert sess.calls[1][:2] == ("GET", "https://hdbk.pmbk.vn/Uploaded/db1/pdf/hd57.pdf")


def test_ok_old_portal(monkeypatch):
    sess = _use(monkeypatch, EMPTY,
                _J({"signedFileName": "hd57.pdf", "databaseName": "BK_KH01", "billStatus": 1,
                    "lookupCode": "a1b2c3d4e5f6"}),
                _Resp(content=_pdf_with("So 57")))
    assert pmbk.PmbkAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    assert sess.calls[1][1] == "https://hoadonbachkhoa.pmbk.vn/api/bill/getByLookupCode/a1b2c3d4e5f6"
    assert sess.calls[2][1] == \
        "https://hoadonbachkhoa.pmbk.vn/uploaded/BK_KH01/pdf/signed/instances/hd57.pdf"


def test_old_portal_deleted_bill(monkeypatch):
    sess = _use(monkeypatch, EMPTY,
                _J({"signedFileName": "hd57.pdf", "databaseName": "BK", "billStatus": 2}),
                _Resp(content=_pdf_with("So 57")))
    pmbk.PmbkAdapter().fetch_pdf(dict(INV))
    assert sess.calls[2][1].endswith("/uploaded/BK/pdf/deleted/hd57.pdf")


def test_not_found_both_portals(monkeypatch):
    _use(monkeypatch, EMPTY, EMPTY)
    with pytest.raises(PdfFetchError) as ei:
        pmbk.PmbkAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        pmbk.PmbkAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


@pytest.mark.parametrize("results, kind", [
    ((_Resp(500, b"x"),), "server"),
    ((requests.Timeout("t"),), "network"),
    ((_J({"path": "a.pdf"}), _Resp(404, b"x")), "no_pdf"),
    ((_J({"path": "a.pdf"}), _Resp(200, b"<html>" + b"x" * 2000, None, "text/html")), "bad_content"),
    ((_J({"path": "a.pdf"}), _Resp(content=_pdf_with("So 99999"))), "bad_content"),  # PDF của HĐ khác
])
def test_errors(monkeypatch, results, kind):
    _use(monkeypatch, *results)
    with pytest.raises(PdfFetchError) as ei:
        pmbk.PmbkAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


def test_fetch_xml(monkeypatch):
    xml = b"<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>57</SHDon></TTChung></DLHDon></HDon>"
    obj = {"hoaDonDienTuId": "g1", "soHoaDon": 57}
    sess = _use(monkeypatch, _J({"path": "x.pdf", "data": {"hoaDonDienTuId": "g1"}}), _J(obj),
                _J({"filePDF": "f/hd.pdf", "fileXML": "f/hd.xml"}), _Resp(content=xml, ctype="text/xml"))
    assert pmbk.PmbkAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[1][1] == f"{NEW_API}/GetById_TraCuu/g1"
    assert sess.calls[2][:2] == ("POST", f"{NEW_API}/TaiHoaDon_TraCuu") and sess.calls[2][2]["json"] == obj
    assert sess.calls[3][1] == "https://hdbk.pmbk.vn/f/hd.xml"


def test_fetch_by_mail_id(monkeypatch):
    sess = _use(monkeypatch, _J({"hoaDonDienTuId": "g1"}), _J({"filePDF": "f/hd.pdf"}),
                _Resp(content=_pdf_with("So 57")))
    inv = pmbk.PmbkAdapter().invoice_from_link(
        "https://hdbk.pmbk.vn/tra-cuu-hoa-don?hoadon=3f2a9c1e-1111-2222-3333-444455556666", None, "")
    inv["shdon"] = 57
    assert pmbk.PmbkAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert sess.calls[0][1].endswith("/GetById_TraCuu/3f2a9c1e-1111-2222-3333-444455556666")
    assert sess.calls[2][1] == "https://hdbk.pmbk.vn/f/hd.pdf"


def test_mail_id_not_found(monkeypatch):
    _use(monkeypatch, _Resp(200, b"", None, ""))
    with pytest.raises(PdfFetchError) as ei:
        pmbk.PmbkAdapter().fetch_pdf({"pmbk_hoadon_id": "bogus-0000-id", "shdon": 1})
    assert ei.value.kind == "no_pdf"


def test_invoice_from_link():
    a = pmbk.PmbkAdapter()
    inv = a.invoice_from_link("https://hdbk.pmbk.vn/tra-cuu-hoa-don/a1b2c3d4e5f6/1", None, "")
    assert inv["ma_tra_cuu"] == "a1b2c3d4e5f6" and inv["pmbk_portal"] == pmbk.NEW
    inv = a.invoice_from_link("https://hoadonbachkhoa.pmbk.vn/tra-cuu-hoa-don", "KX12345678", "")
    assert inv["ma_tra_cuu"] == "KX12345678" and inv["pmbk_portal"] == pmbk.OLD
    assert pmbk.PmbkAdapter._portals(inv) == [pmbk.OLD, pmbk.NEW]
    assert a.invoice_from_link("https://hdbk.pmbk.vn/tra-cuu-hoa-don", None, "") is None
    assert a.invoice_from_link("https://example.com/tra-cuu-hoa-don/a1b2c3d4e5f6", None, "") is None
