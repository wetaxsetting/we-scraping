"""Adapter W-INVOICE Khánh Linh / winvoice.vn (HTTP giả lập, không cần mạng).

Đã kiểm thật 27/10/2026 bằng mã ví dụ in trên trang (C26MTL-571): lookup + PDF + XML.

Run::

    pytest tests/test_pdf_winvoice.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError, adapter_for_host, adapter_for_mst_tcgp   # noqa: E402
from pdf_providers import common                                                    # noqa: E402
from pdf_providers import winvoice                                                  # noqa: E402

CODE = "B6A8F5FE22C84D7B8B2BB44DE0A93530"


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
    def __init__(self, status=200, content=b"", data=None, headers=None):
        self.status_code, self.content, self._data = status, content, data
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace")

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


FOUND = _Resp(data={"success": True, "invoice": {"lookup_code": CODE, "invoice_number": "00000571"}})
NOT_FOUND = _Resp(data={"success": False, "message": "Không tìm thấy hóa đơn hoặc sai mã tra cứu."})
INV = {"shdon": 571, "nbmst": "077175002835", "ma_tra_cuu": None, "tt_khac": {"Mã tra cứu hóa đơn": CODE}}


def _setup(monkeypatch, calls, lookup, download):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw.get("params"), kw.get("files")))
        return lookup if url.endswith("/api/lookup") else download
    monkeypatch.setattr(winvoice, "request", fake)
    monkeypatch.setattr(winvoice, "new_session", lambda: object())


def test_registry():
    assert adapter_for_mst_tcgp("0307633556").source_id == "winvoice"
    assert adapter_for_host("winvoice.vn").source_id == "winvoice"
    assert adapter_for_host("tracuu.wininvoice.vn") is None or \
        adapter_for_host("tracuu.wininvoice.vn").source_id != "winvoice"


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, FOUND, _Resp(content=_pdf_with("So: 00000571")))
    assert winvoice.WInvoiceAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert calls[0] == ("POST", "https://winvoice.vn/api/lookup", None, {"lookup_code": (None, CODE)})
    assert calls[1] == ("GET", f"https://winvoice.vn/winvoice/download/{CODE}", {"doc_type": "pdf", "download": 1},
                        None)


def test_fetch_xml(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, FOUND, _Resp(content="<?xml version='1.0'?><HDon><SHDon>00000571</SHDon></HDon>"
                                            .encode()))
    assert b"SHDon" in winvoice.WInvoiceAdapter().fetch_xml(INV)
    assert calls[1][2] == {"doc_type": "xml", "download": 1}


@pytest.mark.parametrize("lookup, download, kind", [
    (NOT_FOUND, None, "no_pdf"),
    (FOUND, _Resp(404, content="Lỗi kết nối portal".encode()), "no_pdf"),
    (FOUND, _Resp(content=b"<html>" * 400), "bad_content"),
    (FOUND, _Resp(content=_pdf_with("So 99")), "bad_content"),
    (_Resp(502, content=b"bad gateway"), None, "other"),
])
def test_errors(monkeypatch, lookup, download, kind):
    _setup(monkeypatch, [], lookup, download)
    with pytest.raises(PdfFetchError) as ei:
        winvoice.WInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        winvoice.WInvoiceAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = winvoice.WInvoiceAdapter()
    assert a.invoice_from_link("https://winvoice.vn/tra-cuu", None, "") is None
    assert a.invoice_from_link(f"https://winvoice.vn/tra-cuu?lookup_code={CODE}", None, "")["ma_tra_cuu"] == CODE
    assert a.invoice_from_link("https://winvoice.vn/", CODE, "")["tt_khac"] == {"Mã tra cứu hóa đơn": CODE}
