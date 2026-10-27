"""Adapter VININVOICE / tracuu.vininvoice.vn (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 26/10/2026: 3/3 HĐ thật Life Plaza / Châu Hòa tải bằng MCCQT, chữ trùng PDF trong mail.

Run::

    pytest tests/test_pdf_vininvoice.py -q
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
from pdf_providers import vininvoice       # noqa: E402

MCCQT = "006942132551964CC1A0E8111548F60F72"


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

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


NOT_FOUND = _Resp(400, data={"errorCode": 400, "errors": "Không tìm thấy hóa đơn hoặc hóa đơn đã bị xóa"})


def _setup(monkeypatch, calls, routes):
    def fake(session, method, url, **kw):
        calls.append(url)
        for key, resp in routes.items():
            if url.endswith(key):
                return resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(vininvoice, "request", fake)
    monkeypatch.setattr(vininvoice, "new_session", lambda: object())


INV = {"shdon": 14, "khhdon": "C24TLP", "nbmst": "0318117585", "ma_tra_cuu": None, "tt_khac": {}, "ma_cqt": MCCQT}


def test_ok_by_mccqt(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {f"{MCCQT}/pdf": _Resp(content=_pdf_with("So 00000014"))})
    assert vininvoice.VinInvoiceAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert calls == [f"{vininvoice.API}/{MCCQT}/pdf"]


def test_code_first_then_mccqt(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {"94C2359A44624334/pdf": NOT_FOUND, f"{MCCQT}/pdf": _Resp(content=_pdf_with("So 14"))})
    vininvoice.VinInvoiceAdapter().fetch_pdf(dict(INV, ma_tra_cuu="94C2359A44624334"))
    assert [u.rsplit("/", 2)[1] for u in calls] == ["94C2359A44624334", MCCQT]


def test_fetch_xml(monkeypatch):
    _setup(monkeypatch, [], {f"{MCCQT}/xml": _Resp(content=b"<HDon><SHDon>00000014</SHDon></HDon>")})
    assert b"SHDon" in vininvoice.VinInvoiceAdapter().fetch_xml(INV)


def test_fetch_xml_other_invoice(monkeypatch):
    _setup(monkeypatch, [], {f"{MCCQT}/xml": _Resp(content=b"<HDon><SHDon>15</SHDon></HDon>")})
    with pytest.raises(PdfFetchError) as ei:
        vininvoice.VinInvoiceAdapter().fetch_xml(INV)
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("resp, kind", [
    (NOT_FOUND, "no_pdf"),
    (_Resp(content=b"<html>" + b"x" * 2000), "bad_content"),
    (_Resp(content=_pdf_with("So 99")), "bad_content"),
    (_Resp(404), "other"),
])
def test_errors(monkeypatch, resp, kind):
    _setup(monkeypatch, [], {f"{MCCQT}/pdf": resp})
    with pytest.raises(PdfFetchError) as ei:
        vininvoice.VinInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        vininvoice.VinInvoiceAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}, "ma_cqt": None})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = vininvoice.VinInvoiceAdapter()
    assert a.invoice_from_link("https://tracuu.vininvoice.vn/", None, "") is None
    assert a.invoice_from_link("https://tracuu.vininvoice.vn/", "94C2359A44624334", "")["ma_tra_cuu"] \
        == "94C2359A44624334"


def test_declares_mst_tcgp():
    a = vininvoice.VinInvoiceAdapter()
    assert a.source_id == "vininvoice" and a.mst_tcgp == ("0109282176",) and a.hosts == ("tracuu.vininvoice.vn",)
