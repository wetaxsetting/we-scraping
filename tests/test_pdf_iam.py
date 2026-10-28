"""Adapter iam-entry: T-HOADON (tracuu.t-hoadon.vn) + IAM (tracuu.iamsoftware.com.vn), HTTP giả lập — khảo sát 27/10/2026 (chưa có HĐ thật).

Run::

    pytest tests/test_pdf_iam.py -q
"""
from __future__ import annotations

import base64
import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402


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
    def __init__(self, status=200, content=b"", js=None):
        self.status_code, self.content, self._js = status, content, js
        self.headers, self.text = {}, content.decode("utf-8", "replace")

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.results.pop(0)


def _use(monkeypatch, mod, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(mod, "new_session", lambda: sess)
    return sess

from pdf_providers import iam  # noqa: E402

MCCQT = "00513065749EB74F2D8B93BEEDF525E155"
INV = {"shdon": 13, "khhdon": "C26TTH", "nbmst": "0101010101", "mst_tcgp": "0318511655",
       "ma_tra_cuu": None, "tt_khac": {}, "ma_cqt": MCCQT}
NOT_FOUND = {"errorCode": 400, "errors": "Không tìm thấy hóa đơn hoặc hóa đơn đã bị xóa\n"}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0318511655").source_id == "thoadon"
    assert pdf_providers.adapter_for_mst_tcgp("0109193102").source_id == "iamsoftware"
    assert pdf_providers.adapter_for_host("tracuu.t-hoadon.vn").source_id == "thoadon"
    assert pdf_providers.adapter_for_host("tracuu.iamsoftware.com.vn").source_id == "iamsoftware"


@pytest.mark.parametrize("cls, base", [
    (iam.THoaDonAdapter, "https://tracuu.t-hoadon.vn"),
    (iam.IamSoftwareAdapter, "https://tracuu.iamsoftware.com.vn"),
])
def test_ok_by_mccqt(monkeypatch, cls, base):
    sess = _use(monkeypatch, iam, _Resp(content=_pdf_with("So 13")))
    assert cls().fetch_pdf(dict(INV)).startswith(b"%PDF")
    assert sess.calls[0][1] == f"{base}/api/rest/s1/iam-entry/invoices/{MCCQT}/pdf"


def test_own_code_first_then_mccqt(monkeypatch):
    sess = _use(monkeypatch, iam, _Resp(400, js=NOT_FOUND), _Resp(content=_pdf_with("So 13")))
    inv = dict(INV, ma_tra_cuu="94C2359A44624334")
    assert iam.THoaDonAdapter().fetch_pdf(inv).startswith(b"%PDF")
    assert "/94C2359A44624334/pdf" in sess.calls[0][1] and f"/{MCCQT}/pdf" in sess.calls[1][1]


def test_not_found(monkeypatch):
    _use(monkeypatch, iam, _Resp(400, js=NOT_FOUND))
    with pytest.raises(PdfFetchError) as ei:
        iam.THoaDonAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_wrong_pdf(monkeypatch):
    _use(monkeypatch, iam, _Resp(content=_pdf_with("So 999")))
    with pytest.raises(PdfFetchError) as ei:
        iam.THoaDonAdapter().fetch_pdf(dict(INV, shdon=987654))
    assert ei.value.kind == "bad_content"


def test_fetch_xml(monkeypatch):
    xml = b"<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>13</SHDon></TTChung></DLHDon></HDon>"
    sess = _use(monkeypatch, iam, _Resp(content=xml))
    assert iam.IamSoftwareAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[0][1].endswith("/xml")


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        iam.THoaDonAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = iam.THoaDonAdapter()
    assert a.invoice_from_link("https://tracuu.t-hoadon.vn/", "94C2359A44624334", "")["ma_tra_cuu"] == "94C2359A44624334"
    assert a.invoice_from_link("https://tracuu.t-hoadon.vn/", None, "") is None
