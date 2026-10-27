"""Adapter Fahasa / hoadon.fahasa.com (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 26/10/2026: 5/5 mã trong mail (HĐ 5331, 11485, 50580, 128531, 19354) → PDF + XML
có MST người mua 1201496252; gửi token rỗng (cổng tự host không bật reCAPTCHA).

Run::

    pytest tests/test_pdf_fahasa.py -q
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
from pdf_providers import fahasa           # noqa: E402

BASE = fahasa.BASE


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
    def __init__(self, json=None, status=200, text=""):
        self._json, self.status_code, self.text = json, status, text
        self.headers = {}

    def json(self):
        if self._json is None:
            raise ValueError("không phải JSON")
        return self._json


class _Session:
    def __init__(self, routes, calls):
        self.routes, self.calls = routes, calls

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        return self.routes[url.rsplit("/", 1)[-1]]


def _setup(monkeypatch, calls, search=None, xml=None):
    pdf = _pdf_with("So 00019354")
    routes = {
        "search1": search or _Resp({"pdf": "data:application/pdf;base64, " + base64.b64encode(pdf).decode(),
                                    "status": 3}),
        "xml1": xml or _Resp({"xml": "<HDon><SHDon>00019354</SHDon></HDon>"}),
    }
    monkeypatch.setattr(fahasa, "new_session", lambda: _Session(routes, calls))
    monkeypatch.setattr(fahasa, "request", lambda *a, **k: _Resp())


SUBJECT = ("Hóa đơn điện tử số 00019354 – CÔNG TY CỔ PHẦN PHÁT HÀNH SÁCH TPHCM – FAHASA kính gửi "
           "khách hàng CÔNG TY TNHH WEBCASH VIỆT NAM")


def test_link_flow(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    a = fahasa.FahasaAdapter()
    inv = a.invoice_from_link("http://hoadon.fahasa.com/search.html?sec=25C51N63AL&action=search&search=true",
                              "25C51N63AL", SUBJECT)
    assert inv == {"ma_tra_cuu": "25C51N63AL", "nbmst": "0304132047", "tt_khac": {"sec": "25C51N63AL"},
                   "shdon": "19354"}
    assert a.fetch_pdf(dict(inv)).startswith(b"%PDF")
    assert b"<SHDon>00019354</SHDon>" in a.fetch_xml(dict(inv))
    assert calls[0] == (BASE + "/api/seek/search1", {"sec": "25C51N63AL", "action": "search", "token": ""})


def test_pdf_of_other_invoice(monkeypatch):
    _setup(monkeypatch, [])
    with pytest.raises(PdfFetchError) as ei:
        fahasa.FahasaAdapter().fetch_pdf({"ma_tra_cuu": "25C51N63AL", "shdon": "5331"})
    assert ei.value.kind == "bad_content"


def test_not_found(monkeypatch):
    _setup(monkeypatch, [], search=_Resp(status=500, text="Không tìm thấy hóa đơn mã tra cứu: XXXXXXXXXX"))
    with pytest.raises(PdfFetchError) as ei:
        fahasa.FahasaAdapter().fetch_pdf({"ma_tra_cuu": "XXXXXXXXXX"})
    assert ei.value.kind == "no_pdf"


def test_server_error(monkeypatch):
    _setup(monkeypatch, [], search=_Resp(status=502, text="Bad gateway"))
    with pytest.raises(PdfFetchError) as ei:
        fahasa.FahasaAdapter().fetch_pdf({"ma_tra_cuu": "25C51N63AL"})
    assert ei.value.kind == "server"


def test_xml_other_number(monkeypatch):
    _setup(monkeypatch, [], xml=_Resp({"xml": "<HDon><SHDon>1</SHDon></HDon>"}))
    with pytest.raises(PdfFetchError) as ei:
        fahasa.FahasaAdapter().fetch_xml({"ma_tra_cuu": "25C51N63AL", "shdon": 19354})
    assert ei.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        fahasa.FahasaAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link_other():
    a = fahasa.FahasaAdapter()
    inv = a.invoice_from_link("http://hoadon.fahasa.com/search.html", "ID05F14662", "FW: không có số")
    assert inv["ma_tra_cuu"] == "ID05F14662" and "shdon" not in inv
    assert a.invoice_from_link("http://hoadon.fahasa.com/search.html", None, SUBJECT) is None
    assert a.invoice_from_link("https://tracuuhoadon.fpt.com.vn/?sec=25C51N63AL", None, "") is None


def test_declares():
    a = fahasa.FahasaAdapter()
    assert a.source_id == "fahasa" and a.mst_tcgp == () and a.hosts == ("hoadon.fahasa.com",)
