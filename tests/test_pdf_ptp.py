"""Adapter PTP-Invoice (hoadondientu-ptp.vn), HTTP giả lập — khảo sát 27/10/2026, chưa có HĐ thật.

Run::

    pytest tests/test_pdf_ptp.py -q
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


from pdf_providers import ptp  # noqa: E402

INV = {"shdon": 123, "khhdon": "C26TPT", "nbmst": "0100687474-001", "mst_tcgp": "0100687474",
       "tt_khac": {"Số bảo mật": "AB12-CD34"}}


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(ptp, "new_session", lambda: sess)
    return sess


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0100687474").source_id == "ptp"
    assert pdf_providers.adapter_for_host("hoadondientu-ptp.vn").source_id == "ptp"


def test_ok_direct(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 123")))
    assert ptp.PtpAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "https://hoadondientu-ptp.vn/Search/SearchInvoice"
    assert kw["json"] == {"masothue": "0100687474001", "sobaomat": "AB12CD34", "type": "pdf"}
    assert kw["headers"]["Accept"] == "application/pdf"


def test_fallback_after_417(monkeypatch):
    sess = _use(monkeypatch, _Resp(417, b"SQL Server error", ctype="text/plain"),
                _Resp(content=_pdf_with("So 123")))
    assert ptp.PtpAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[1]
    assert method == "GET" and url == "https://hoadondientu-ptp.vn/Tracuu/ptp-Invoice/"
    assert kw["params"] == {"mst": "0100687474001", "id": "AB12CD34"}


def test_not_found(monkeypatch):
    _use(monkeypatch, _Resp(417, b"x", ctype="text/plain"),
         _Resp(content="Không tồn tại số bảo mật".encode(), ctype="text/plain"))
    with pytest.raises(PdfFetchError) as ei:
        ptp.PtpAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_wrong_invoice(monkeypatch):
    _use(monkeypatch, _Resp(content=_pdf_with("So 99999")))
    with pytest.raises(PdfFetchError) as ei:
        ptp.PtpAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [{"shdon": 1, "nbmst": "0100687474", "tt_khac": {}},
                                 {"shdon": 1, "nbmst": "", "tt_khac": {"Số bảo mật": "X1"}}])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        ptp.PtpAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_fetch_xml(monkeypatch):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hd.xml", "<HDon><DLHDon><TTChung><SHDon>123</SHDon></TTChung></DLHDon></HDon>")
    _use(monkeypatch, _Resp(content=buf.getvalue(), ctype="application/zip"))
    assert b"<SHDon>123" in ptp.PtpAdapter().fetch_xml(dict(INV))
    _use(monkeypatch, _Resp(content=b"A network-related ... SQL Server ...", ctype="application/zip"))
    with pytest.raises(PdfFetchError) as ei:
        ptp.PtpAdapter().fetch_xml(dict(INV))
    assert ei.value.kind == "server"


def test_invoice_from_link():
    a = ptp.PtpAdapter()
    inv = a.invoice_from_link("https://hoadondientu-ptp.vn/Tracuu/ptp-Invoice/?mst=0101234567&id=ABC123",
                              None, "")
    assert inv["nbmst"] == "0101234567" and inv["ma_tra_cuu"] == "ABC123"
    assert a.invoice_from_link("https://hoadondientu-ptp.vn/tra-cuu/", "ABC", "") is None
    assert a.invoice_from_link("https://example.com/?mst=1&id=2", None, "") is None
