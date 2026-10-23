"""Adapter Vĩnh Hy (ehoadondientu.com) + Minh Khang (hoadondientuvat.com), HTTP giả lập —
luồng thật đã kiểm 23/10/2026.

Run::

    pytest tests/test_pdf_vinhhy.py -q
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
from pdf_providers import common, minhkhang, vinhhy    # noqa: E402


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
        res = self.results.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(vinhhy, "new_session", lambda: sess)
    return sess


INV = {"shdon": 2087, "khhdon": "C24TPR", "khmshdon": 1, "nbmst": "0315800401",
       "mst_tcgp": "0314743623", "ma_tra_cuu": None, "tt_khac": {},
       "dlhdon_id": "031580040120190300029085"}
INV_MK = {"shdon": 23, "khhdon": "C24TLV", "khmshdon": 1, "nbmst": "0316994446",
          "mst_tcgp": "0314209362", "tt_khac": {}, "dlhdon_id": "031699444620220300000675"}
# Trang kết quả tra cứu thật (rút gọn); JSON đã giải mã ' → '
FOUND_HTML = ("<table><tr><td style='display:none'><span id='lblData'>03158004012019<span></td>"
              "<td style='display:none'><span id='lblStt'>0300023460<span></td></tr></table>"
              "<a href='#' onclick=\"generate_file('ct_03158004012019_0300023460','xml')\">Xml</a>"
              "<a href='#' onclick=\"generate_file('ct_03158004012019_0300023460','pdf') ;  \">PDF</a>")


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0314743623").source_id == "vinhhy"
    assert pdf_providers.adapter_for_mst_tcgp("0314209362").source_id == "minhkhang"
    assert pdf_providers.adapter_for_host("ehoadondientu.com").source_id == "vinhhy"
    assert pdf_providers.adapter_for_host("hoadondientuvat.com").source_id == "minhkhang"


@pytest.mark.parametrize("dlhdon_id, ref", [
    ("031580040120190300029085", "ct_03158004012019_0300029085"),
    ("03158004010012019" + "0300000001", "ct_03158004010012019_0300000001"),  # MST chi nhánh
    ("", None), (None, None), ("0315800401201903", None), ("ABC80040120190300029085X", None),
])
def test_ref_from_id(dlhdon_id, ref):
    assert vinhhy.VinhHyAdapter.ref_from_id(dlhdon_id) == ref


def test_ok_by_dlhdon_id(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 2087")))
    assert vinhhy.VinhHyAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    assert len(sess.calls) == 1
    method, url, kw = sess.calls[0]
    assert method == "GET" and url == "https://ehoadondientu.com/Account/GenerateFile.aspx"
    assert kw["params"] == {"r": "ct_03158004012019_0300029085", "type": "pdf"}


def test_minhkhang_uses_own_host(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 23")))
    assert minhkhang.MinhKhangAdapter().fetch_pdf(dict(INV_MK)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert url == "https://hoadondientuvat.com/Account/GenerateFile.aspx"
    assert kw["params"] == {"r": "ct_03169944462022_0300000675", "type": "pdf"}


def test_ok_by_mail_code(monkeypatch):
    sess = _use(monkeypatch, _Resp(js={"d": FOUND_HTML}, ctype="application/json"),
                _Resp(content=_pdf_with("So 4605")))
    inv = {"shdon": 4605, "ma_tra_cuu": "023NUKHLOZR", "tt_khac": {}}
    assert vinhhy.VinhHyAdapter().fetch_pdf(inv).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "https://ehoadondientu.com/Tracuu.aspx/tracuu"
    assert kw["json"]["mahoadon"] == "023NUKHLOZR"
    assert sess.calls[1][2]["params"] == {"r": "ct_03158004012019_0300023460", "type": "pdf"}


def test_fetch_xml(monkeypatch):
    xml = "<HDon><DLHDon><TTChung><SHDon>2087</SHDon></TTChung></DLHDon></HDon>".encode()
    sess = _use(monkeypatch, _Resp(content=xml, ctype="text/html"))
    assert vinhhy.VinhHyAdapter().fetch_xml(dict(INV)) == xml
    assert sess.calls[0][2]["params"]["type"] == "xml"
    _use(monkeypatch, _Resp(content=xml.replace(b"2087", b"9999"), ctype="text/html"))
    with pytest.raises(PdfFetchError) as ei:
        vinhhy.VinhHyAdapter().fetch_xml(dict(INV))
    assert ei.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        vinhhy.VinhHyAdapter().fetch_pdf({"shdon": 1, "dlhdon_id": "", "tt_khac": {}})
    assert ei.value.kind == "no_code"


def test_mail_code_not_found(monkeypatch):
    _use(monkeypatch, _Resp(js={"d": ""}, ctype="application/json"))
    with pytest.raises(PdfFetchError) as ei:
        vinhhy.VinhHyAdapter().fetch_pdf({"shdon": 1, "ma_tra_cuu": "ZZZZZZZZZZ"})
    assert ei.value.kind == "no_pdf"


@pytest.mark.parametrize("result, kind", [
    (_Resp(200, b"", None, "text/html"), "no_pdf"),            # HĐ không có: 200 + thân rỗng
    (_Resp(500, b"loi", None, "text/html"), "server"),
    (_Resp(429, b""), "rate_limit"),
    (_Resp(404, b"x"), "other"),
    (requests.Timeout("t"), "network"),
    (requests.ConnectionError("c"), "network"),
    (_Resp(200, b"<html>" + b"x" * 2000, None, "text/html"), "bad_content"),
    (_Resp(200, _pdf_with("So 99999")), "bad_content"),      # PDF của HĐ khác
])
def test_errors(monkeypatch, result, kind):
    _use(monkeypatch, result)
    with pytest.raises(PdfFetchError) as ei:
        vinhhy.VinhHyAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


def test_invoice_from_link():
    a = vinhhy.VinhHyAdapter()
    inv = a.invoice_from_link("https://ehoadondientu.com/Tracuu.aspx?ma=Q2EZFMHZMY&e=a@b.com&l=Vn",
                              None, "")
    assert inv["ma_tra_cuu"] == "Q2EZFMHZMY" and inv["mst_tcgp"] == "0314743623"
    assert a.invoice_from_link("https://ehoadondientu.com/Tra-cuu", "023NUKHLOZR", "")["ma_tra_cuu"] \
        == "023NUKHLOZR"
    assert a.invoice_from_link("https://ehoadondientu.com/Tra-cuu", None, "") is None
    assert a.invoice_from_link("https://example.com/Tracuu.aspx?ma=Q2EZFMHZMY", None, "") is None
    mk = minhkhang.MinhKhangAdapter().invoice_from_link(
        "https://hoadondientuvat.com/Tracuu.aspx?ma=NWD2S2F0N5", None, "")
    assert mk["mst_tcgp"] == "0314209362"
