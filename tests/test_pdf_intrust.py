"""Adapter INTRUST / SCHOOL TECH (lookupeinvoice.intrustdss.vn), HTTP giả lập — khảo sát 27/10/2026.

Run::

    pytest tests/test_pdf_intrust.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common, intrust  # noqa: E402


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


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(intrust, "new_session", lambda: sess)
    return sess


NOT_FOUND = {"status": "error", "code": "HE035", "message": "Thông tin tra cứu không đúng bạn vui lòng thử lại",
             "data": {"issued_date_converted": "01/01/1970"}}
FOUND = {"status": "success", "data": {
    "id": 77, "invoice_number": "0000123", "payment_status": True, "check_payment_status": False,
    "pdf_file": {"link": "https://lookupeinvoice.intrustdss.vn/view-pdf/77"},
    "attach_file": [{"code": "HD01", "name": "hd.xml", "link": "/files/77.xml"},
                    {"code": "HD02", "name": "hd.pdf", "link": "/files/77.pdf"}]}}
INV = {"shdon": 123, "khhdon": "C26TAA", "nbmst": "0101010101", "mst_tcgp": "0110333410",
       "ma_tra_cuu": "4G7ABC9XYZ", "tt_khac": {}}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0110333410").source_id == "intrust"
    assert pdf_providers.adapter_for_mst_tcgp("0317509886").source_id == "intrust"
    assert pdf_providers.adapter_for_host("lookupeinvoice.intrustdss.vn").source_id == "intrust"


def test_lookup_codes_from_tt_khac():
    inv = {"ma_tra_cuu": None, "tt_khac": {"Mã tra cứu hóa đơn": "AB12CD34", "Ghi chú": "x"}}
    assert intrust.lookup_codes(inv) == ["AB12CD34"]


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _Resp(js=FOUND), _Resp(content=_pdf_with("So 123")))
    assert intrust.IntrustAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "https://lookupeinvoice.intrustdss.vn/kiem-tra/check"
    assert kw["json"]["signature"] == "4G7ABC9XYZ" and kw["json"]["gRecaptchaResponse"] is None
    assert sess.calls[1][1] == "https://lookupeinvoice.intrustdss.vn/files/77.pdf"


def test_fetch_xml(monkeypatch):
    xml = b"<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>123</SHDon></TTChung></DLHDon></HDon>"
    sess = _use(monkeypatch, _Resp(js=FOUND), _Resp(content=xml))
    assert intrust.IntrustAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[1][1].endswith("/files/77.xml")


def test_not_found(monkeypatch):
    _use(monkeypatch, _Resp(js=NOT_FOUND))
    with pytest.raises(PdfFetchError) as ei:
        intrust.IntrustAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_require_recaptcha_stops(monkeypatch):
    _use(monkeypatch, _Resp(js={"status": "error", "require_captcha": 1, "message": "captcha"}))
    with pytest.raises(PdfFetchError) as ei:
        intrust.IntrustAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "rate_limit"


def test_unpaid(monkeypatch):
    js = {"status": "success", "data": dict(FOUND["data"], payment_status=False, check_payment_status=True)}
    _use(monkeypatch, _Resp(js=js))
    with pytest.raises(PdfFetchError) as ei:
        intrust.IntrustAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_wrong_pdf(monkeypatch):
    _use(monkeypatch, _Resp(js=FOUND), _Resp(content=_pdf_with("So 999")))
    with pytest.raises(PdfFetchError) as ei:
        intrust.IntrustAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        intrust.IntrustAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = intrust.IntrustAdapter()
    inv = a.invoice_from_link("https://lookupeinvoice.intrustdss.vn/?mst=0105890046&shd=0000388&mtc=4G709U2VFH",
                              None, "")
    assert inv["ma_tra_cuu"] == "4G709U2VFH" and inv["nbmst"] == "0105890046" and inv["shdon"] == 388
    assert a.invoice_from_link("https://lookupeinvoice.intrustdss.vn/", None, "") is None
    assert a.invoice_from_link("https://lookupeinvoice.intrustdss.vn/", "ABCD1234", "")["ma_tra_cuu"] == "ABCD1234"
