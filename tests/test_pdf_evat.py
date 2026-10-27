"""Adapter eVAT / phuongnam.evat.vn (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 26/10/2026: 4/4 HĐ thật Universal Orient Two (C24TAA-30/31/38/39) tải được ZIP,
PDF trong ZIP trùng chữ PDF trong mail; không cần reCAPTCHA.

Run::

    pytest tests/test_pdf_evat.py -q
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import evat             # noqa: E402

BASE = "https://phuongnam.evat.vn"
DL = "/bill/view?name=C24TAA_00000031&amp;f=ABC%2F&amp;download=1"


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


def _zip(pdf=None, xml=b"<HDon><SHDon>31</SHDon></HDon>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("C24TAA_00000031signed.xslt", b"<xsl/>")
        if pdf is not False:
            z.writestr("C24TAA_00000031signed.pdf", pdf or _pdf_with("So 31"))
        z.writestr("C24TAA_00000031signed.xml", xml)
    return buf.getvalue()


class _Resp:
    def __init__(self, text="", status=200, content=b"", url=BASE + "/bill", headers=None):
        self.text, self.status_code, self.content, self.url = text, status, content, url
        self.headers = headers or {}


HOME = _Resp('<form class="login-form" action="/bill" method="post"><input name="private_code"/>'
             '<input type="hidden" name="phtcsrf_tkns-tad"  value="tok123"/>'
             '<input type="hidden" name="test" value="1" /></form>', url=BASE + "/")
FOUND = _Resp(f'<table><tr><td><a href="/bill/view?f=X&amp;is_db=1">Xem</a><a href="{DL}" title="Tải">T</a>'
              '</td></tr></table>', url=BASE + "/bill/inv")
NOT_FOUND = _Resp('<div class="login-error"><b>ERROR: </b> Không tìm thấy hóa đơn</div>')
NO_COMPANY = _Resp('<div class="login-error"><b>ERROR: </b> Mã công ty không tồn tại</div>')


def _setup(monkeypatch, calls, result=FOUND, download=None):
    download = download if download is not None else _Resp(content=_zip())

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET" and url == BASE + "/":
            return HOME
        if method == "POST" and url == BASE + "/bill":
            return result
        if url.startswith(BASE + "/bill/view?name="):
            return download
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(evat, "request", fake)
    monkeypatch.setattr(evat, "new_session", lambda: object())


INV = {"shdon": 31, "khhdon": "C24TAA", "khmshdon": 1, "nbmst": "0313632864", "mst_tcgp": "0316642395",
       "ma_tra_cuu": None, "tt_khac": {"Mã tra cứu hóa đơn": "TC6VO2832L", "Mã công ty": "0313632864"}}


def test_ok(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    assert evat.EvatAdapter().fetch_pdf(INV).startswith(b"%PDF")
    post = [kw for m, _, kw in calls if m == "POST"][0]
    assert post["data"] == {"private_code": "TC6VO2832L", "cmpn_key": "0313632864", "action": "submit",
                            "phtcsrf_tkns-tad": "tok123", "test": "1"}
    dl = [u for m, u, _ in calls if "download=1" in u][0]
    assert dl == BASE + "/bill/view?name=C24TAA_00000031&f=ABC%2F&download=1"


def test_fetch_xml(monkeypatch):
    _setup(monkeypatch, [])
    assert b"<SHDon>31</SHDon>" in evat.EvatAdapter().fetch_xml(INV)


def test_xml_of_other_invoice(monkeypatch):
    _setup(monkeypatch, [], download=_Resp(content=_zip(xml=b"<HDon><SHDon>00000032</SHDon></HDon>")))
    with pytest.raises(PdfFetchError) as ei:
        evat.EvatAdapter().fetch_xml(INV)
    assert ei.value.kind == "bad_content"


def test_company_key_falls_back_to_seller_mst(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    inv = dict(INV, tt_khac={"Mã tra cứu hóa đơn": "TC6VO2832L"})
    evat.EvatAdapter().fetch_pdf(inv)
    assert [kw for m, _, kw in calls if m == "POST"][0]["data"]["cmpn_key"] == "0313632864"


@pytest.mark.parametrize("result, kind", [(NOT_FOUND, "no_pdf"), (NO_COMPANY, "no_portal"),
                                          (_Resp("<html>trang lạ</html>"), "bad_content")])
def test_lookup_errors(monkeypatch, result, kind):
    _setup(monkeypatch, [], result=result)
    with pytest.raises(PdfFetchError) as ei:
        evat.EvatAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


@pytest.mark.parametrize("download, kind", [
    (_Resp("<html>" + "x" * 2000, headers={"Content-Type": "text/html"}), "bad_content"),   # không phải ZIP
    ("other_invoice", "bad_content"),
    (_Resp(content=_zip(pdf=False)), "no_pdf"),
    (_Resp(status=404), "bad_content"),
])
def test_download_errors(monkeypatch, download, kind):
    if download == "other_invoice":
        download = _Resp(content=_zip(pdf=_pdf_with("So 99999")))
    _setup(monkeypatch, [], download=download)
    with pytest.raises(PdfFetchError) as ei:
        evat.EvatAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


@pytest.mark.parametrize("inv, kind", [
    ({"shdon": 1, "mst_tcgp": "0316642395", "nbmst": "0313632864", "tt_khac": {}}, "no_code"),
    ({"shdon": 1, "mst_tcgp": "0316642395", "nbmst": "", "tt_khac": {"Mã tra cứu hóa đơn": "TC6VO2832L"}},
     "no_code"),
    ({"shdon": 1, "mst_tcgp": "0999999999", "nbmst": "0313632864",
      "tt_khac": {"Mã tra cứu hóa đơn": "TC6VO2832L"}}, "no_portal"),
])
def test_missing_inputs(inv, kind):
    with pytest.raises(PdfFetchError) as ei:
        evat.EvatAdapter().fetch_pdf(inv)
    assert ei.value.kind == kind


def test_portal_from_lookup_url():
    a = evat.EvatAdapter()
    assert a.portal({"mst_tcgp": "0316642395"}) == "phuongnam.evat.vn"
    assert a.portal({"mst_tcgp": "0315191291", "lookup_url": "https://hoadonsovn.evat.vn/"}) == "hoadonsovn.evat.vn"
    assert a.portal({"mst_tcgp": "0312303803", "lookup_url": "http://tracuu.evat.vn"}) == ""   # nay là WinInvoice


def test_invoice_from_link():
    a = evat.EvatAdapter()
    assert a.invoice_from_link("http://phuongnam.evat.vn", "TC6VO2832L", "") is None      # thiếu mã công ty
    inv = a.invoice_from_link("http://phuongnam.evat.vn", "TC6VO2832L", "mã tra cứu: TC6VO2832L , mã công ty: 0313632864")
    assert inv["nbmst"] == "0313632864" and inv["mst_tcgp"] == "0316642395"
    assert inv["tt_khac"]["Mã tra cứu hóa đơn"] == "TC6VO2832L"
    assert a.invoice_from_link("http://tracuu.evat.vn", "742800EC42", "mã công ty: 0311767270") is None


def test_declares_mst_tcgp():
    a = evat.EvatAdapter()
    assert a.source_id == "evat" and a.mst_tcgp == ("0316642395",) and a.hosts == ("phuongnam.evat.vn",)
