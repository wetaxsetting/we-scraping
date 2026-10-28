"""Nhà bán lại cùng phần mềm Vĩnh Hy (Tia Lửa Việt, Tuần Châu, Minh Thư, HT Sài Gòn), HTTP giả lập.

Khảo sát 27/10/2026: cả 4 host trả đúng dạng "không tìm thấy" của vinhhy; chưa có HĐ thật.

Run::

    pytest tests/test_pdf_vinhhy_resellers.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                              # noqa: E402
from pdf_providers import PdfFetchError                           # noqa: E402
from pdf_providers import common, vinhhy                          # noqa: E402
from pdf_providers import htsaigon, minhthu, tialuaviet, tuanchau  # noqa: E402

CASES = [
    (tialuaviet.TiaLuaVietAdapter, "tialuaviet", "0303609305", "https://ihoadondientu.com", "/Tra-cuu"),
    (tuanchau.TuanChauAdapter, "tuanchau", "0400462489", "https://e-invoicetuanchau.com", "/Tra-cuu"),
    (minhthu.MinhThuAdapter, "minhthu", "3500456910", "https://hoadonminhthuvungtau.com", "/Tra-cuu"),
    (htsaigon.HtSaiGonAdapter, "htsaigon", "0312942260", "https://ihoadondientu.net", "/Tracuu.aspx"),
]


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
        return self.results.pop(0)


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(vinhhy, "new_session", lambda: sess)
    return sess


def _inv(mst_tcgp, **kw):
    inv = {"shdon": 77, "khhdon": "C26TAA", "khmshdon": 1, "nbmst": "0301234567",
           "mst_tcgp": mst_tcgp, "ma_tra_cuu": None, "tt_khac": {},
           "dlhdon_id": "030123456720260300000077"}
    inv.update(kw)
    return inv


@pytest.mark.parametrize("cls, sid, mst, base, page", CASES)
def test_registry_and_host(cls, sid, mst, base, page):
    a = cls()
    assert isinstance(a, vinhhy.VinhHyAdapter)
    assert pdf_providers.adapter_for_mst_tcgp(mst).source_id == sid
    host = base.split("//", 1)[1]
    assert pdf_providers.adapter_for_host(host).source_id == sid
    assert a.BASE == base and a.LOOKUP_PAGE == page


def test_hosts_do_not_collide():
    # ihoadondientu.com khác ehoadondientu.com (Vĩnh Hy); ihoadondientu.net khác .com
    assert pdf_providers.adapter_for_host("ehoadondientu.com").source_id == "vinhhy"
    assert pdf_providers.adapter_for_host("ihoadondientu.com").source_id == "tialuaviet"
    assert pdf_providers.adapter_for_host("ihoadondientu.net").source_id == "htsaigon"


@pytest.mark.parametrize("cls, sid, mst, base, page", CASES)
def test_pdf_from_dlhdon_id_on_own_host(monkeypatch, cls, sid, mst, base, page):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("C26TAA 77 0301234567")))
    pdf = cls().fetch_pdf(_inv(mst))
    assert pdf.startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert (method, url) == ("GET", f"{base}/Account/GenerateFile.aspx")
    assert kw["params"] == {"r": "ct_03012345672026_0300000077", "type": "pdf"}
    assert kw["headers"]["Referer"] == base + page


@pytest.mark.parametrize("cls, sid, mst, base, page", CASES)
def test_not_found_empty_body(monkeypatch, cls, sid, mst, base, page):
    _use(monkeypatch, _Resp(content=b"", ctype="text/html"))
    with pytest.raises(PdfFetchError) as e:
        cls().fetch_pdf(_inv(mst))
    assert e.value.kind == "no_pdf"


@pytest.mark.parametrize("cls, sid, mst, base, page", CASES)
def test_lookup_code_not_found(monkeypatch, cls, sid, mst, base, page):
    sess = _use(monkeypatch, _Resp(content=b'{"d":""}', js={"d": ""}, ctype="application/json"))
    with pytest.raises(PdfFetchError) as e:
        cls().fetch_pdf(_inv(mst, dlhdon_id=None, ma_tra_cuu="ZZZZZZZZZZ"))
    assert e.value.kind == "no_pdf"
    method, url, kw = sess.calls[0]
    assert (method, url) == ("POST", f"{base}/Tracuu.aspx/tracuu")
    assert kw["json"] == {"mahoadon": "ZZZZZZZZZZ", "loai": "mhd", "lang": "Vn"}


@pytest.mark.parametrize("cls, sid, mst, base, page", CASES)
def test_no_code(monkeypatch, cls, sid, mst, base, page):
    _use(monkeypatch)
    with pytest.raises(PdfFetchError) as e:
        cls().fetch_pdf(_inv(mst, dlhdon_id=None))
    assert e.value.kind == "no_code"


@pytest.mark.parametrize("cls, sid, mst, base, page", CASES)
def test_invoice_from_link(cls, sid, mst, base, page):
    a = cls()
    got = a.invoice_from_link(f"{base}/Tracuu.aspx?ma=Q2EZFMHZMY&e=x@y.vn&l=Vn", None, "")
    assert got["mst_tcgp"] == mst and got["ma_tra_cuu"] == "Q2EZFMHZMY"
    assert a.invoice_from_link("https://ehoadondientu.com/Tracuu.aspx?ma=Q2EZFMHZMY", None, "") is None
    assert a.invoice_from_link(f"{base}{page}", None, "") is None
