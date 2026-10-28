"""Adapter VietInfo (tracuuhoadon.vietinfo.tech), HTTP giả lập — khảo sát 27/10/2026, chưa có HĐ thật.

Run::

    pytest tests/test_pdf_vietinfo.py -q
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
    def __init__(self, status=200, content=b"", js=None, ctype="application/pdf", url=""):
        self.status_code, self.content, self._js, self.url = status, content, js, url
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


def _html(s: str) -> _Resp:
    return _Resp(content=s.encode(), ctype="text/html; charset=utf-8")


import base64  # noqa: E402
import datetime as dt  # noqa: E402

from pdf_providers import vietinfo  # noqa: E402

INV = {"shdon": 45, "nbmst": "0312345678", "mst_tcgp": "0311928954", "ngay_lap": "2026-10-03",
       "tt_khac": {"Mã tra cứu": "VI45ABC"}}
FOUND = ('<table><tbody><tr><td>45</td><td><input type="hidden" value="/Files/2026/HD_45.xml" '
         'id="inputFileHoaDon" /><input id="inputHoaDonId" type="hidden" value="991" /></td></tr></tbody></table>')
EMPTY = "<table><tbody>\n</tbody></table>"


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(vietinfo, "new_session", lambda: sess)
    return sess


def _b64(data: bytes) -> _Resp:
    return _Resp(content=b'"' + base64.b64encode(data) + b'"', ctype="application/json")


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0311928954").source_id == "vietinfo"
    assert pdf_providers.adapter_for_host("tracuuhoadon.vietinfo.tech").source_id == "vietinfo"


@pytest.mark.parametrize("value, out", [("2026-10-03", "03/10/2026"), (dt.date(2026, 1, 5), "05/01/2026"),
                                        ("3/1/2026", "03/01/2026"), ("", None), (None, None)])
def test_date_vn(value, out):
    assert vietinfo.date_vn(value) == out


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _html(FOUND), _b64(_pdf_with("So 45")))
    assert vietinfo.VietInfoAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "https://tracuuhoadon.vietinfo.tech/"
    assert kw["data"] == {"MaTraCuu": "VI45ABC", "NgayHoaDon": "03/10/2026", "btnSearch": ""}
    method, url, kw = sess.calls[1]
    assert url == "https://tracuuhoadon.vietinfo.tech/Home/TaiFileHoaDon"
    assert kw["params"] == {"FileHoaDon": "/Files/2026/HD_45.pdf"}


def test_fetch_xml(monkeypatch):
    xml = b"<?xml version='1.0'?><HDon><DLHDon><TTChung><SHDon>45</SHDon></TTChung></DLHDon></HDon>"
    sess = _use(monkeypatch, _html(FOUND), _b64(xml))
    assert vietinfo.VietInfoAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[1][2]["params"] == {"FileHoaDon": "/Files/2026/HD_45.xml"}


def test_not_found(monkeypatch):
    _use(monkeypatch, _html(EMPTY))
    with pytest.raises(PdfFetchError) as ei:
        vietinfo.VietInfoAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_bad_base64(monkeypatch):
    _use(monkeypatch, _html(FOUND), _Resp(content=b"<html>loi</html>", ctype="text/html"))
    with pytest.raises(PdfFetchError) as ei:
        vietinfo.VietInfoAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [{"shdon": 1, "ngay_lap": "2026-10-03", "tt_khac": {}},
                                 {"shdon": 1, "ngay_lap": None, "ma_tra_cuu": "X"}])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        vietinfo.VietInfoAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    a = vietinfo.VietInfoAdapter()
    inv = a.invoice_from_link("https://tracuuhoadon.vietinfo.tech/", "VI45ABC", "Ngày hóa đơn: 03/10/2026")
    assert inv["ma_tra_cuu"] == "VI45ABC" and inv["ngay_lap"] == "03/10/2026"
    assert a.invoice_from_link("https://tracuuhoadon.vietinfo.tech/", "VI45ABC", "không có ngày") is None
    assert a.invoice_from_link("https://tracuuhoadon.vietinfo.tech/", "VI45ABC",
                               "01/10/2026 và 03/10/2026") is None
