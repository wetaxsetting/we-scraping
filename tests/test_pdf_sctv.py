"""Adapter SCTV (tracuu-hddt.sctv.vn), HTTP giả lập — khảo sát 27/10/2026 (chưa có HĐ thật).

Run::

    pytest tests/test_pdf_sctv.py -q
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

from pdf_providers import sctv  # noqa: E402

INV = {"shdon": 4521, "khhdon": "C26TST", "nbmst": "0301463315", "mst_tcgp": "0301463315",
       "ma_tra_cuu": "SCTV0001AB", "tt_khac": {}}
FOUND = {"isTT32": "0", "data": [
    {"khieu": "1C26TST", "shdon": 4520, "sbmat": "OTHERSBM", "mst": "0301463315"},
    {"khieu": "1C26TST", "shdon": 4521, "sbmat": "SBM4521", "mst": "0301463315"}]}


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0301463315").source_id == "sctv"
    assert pdf_providers.adapter_for_host("tracuu-hddt.sctv.vn").source_id == "sctv"


def test_ok_picks_matching_row(monkeypatch):
    sess = _use(monkeypatch, sctv, _Resp(js=FOUND), _Resp(content=_pdf_with("So 4521")))
    assert sctv.SctvAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    m, url, kw = sess.calls[0]
    assert m == "POST" and url == "https://tracuu-hddt.sctv.vn/Tracuu2/GetInfoInvoice"
    assert kw["json"] == {"sobaomat": "SCTV0001AB", "masothue": "172"}
    assert sess.calls[1][1].endswith("/Tracuu2/PrintInvoice")
    assert sess.calls[1][2]["json"] == {"sobaomat": "SBM4521", "masothue": "0301463315", "type": "PDF"}


def test_tt32_rows(monkeypatch):
    js = {"isTT32": "1", "data": [{"invoice_number": "0004521", "security_number": "S32", "mst": "0301463315"}]}
    sess = _use(monkeypatch, sctv, _Resp(js=js), _Resp(content=_pdf_with("So 4521")))
    sctv.SctvAdapter().fetch_pdf(dict(INV))
    assert sess.calls[1][2]["json"]["sobaomat"] == "S32"


def test_not_found(monkeypatch):
    _use(monkeypatch, sctv, _Resp(js={"error": "Không tồn tại hóa đơn có Mã tra cứu hoá đơn: SCTV0001AB"}))
    with pytest.raises(PdfFetchError) as ei:
        sctv.SctvAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_no_matching_number(monkeypatch):
    _use(monkeypatch, sctv, _Resp(js=FOUND))
    with pytest.raises(PdfFetchError) as ei:
        sctv.SctvAdapter().fetch_pdf(dict(INV, shdon=1))
    assert ei.value.kind == "no_pdf"


def test_wrong_pdf(monkeypatch):
    _use(monkeypatch, sctv, _Resp(js=FOUND), _Resp(content=_pdf_with("So 999")))
    with pytest.raises(PdfFetchError) as ei:
        sctv.SctvAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_fetch_xml(monkeypatch):
    xml = b"<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>4521</SHDon></TTChung></DLHDon></HDon>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hd_4521.xml", xml)
    sess = _use(monkeypatch, sctv, _Resp(js=FOUND), _Resp(content=buf.getvalue()))
    assert sctv.SctvAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[1][1].endswith("/Tracuu2/ExportZipFileXML")


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        sctv.SctvAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_code_from_tt_khac():
    assert sctv.lookup_codes({"tt_khac": {"Số bảo mật": "ABC12345"}}) == ["ABC12345"]


def test_invoice_from_link():
    a = sctv.SctvAdapter()
    assert a.invoice_from_link("https://tracuu-hddt.sctv.vn/?sobaomat=XY12345", None, "")["ma_tra_cuu"] == "XY12345"
    assert a.invoice_from_link("https://tracuu-hddt.sctv.vn/", None, "") is None
