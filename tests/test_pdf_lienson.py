"""Nhà bán lại nền tảng xcyber: Liên Sơn (API riêng) + My-Invoice 0106820789 (API bill2app), HTTP giả lập.

Khảo sát 27/10/2026: Liên Sơn trả status 4 cho mã bịa sau khi model g_300x75 đọc đúng captcha,
status 2 cho captcha sai. Chưa có HĐ thật.

Run::

    pytest tests/test_pdf_lienson.py -q
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                            # noqa: E402
from pdf_providers import PdfFetchError                         # noqa: E402
from pdf_providers import common, lienson, myinvoicevn, xcyber  # noqa: E402

LS_API = "https://ls-invoiceapp.lienson.vn" + xcyber.API_PATH
B2_API = "https://bill2app.xcyber.vn" + xcyber.API_PATH


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
    def __init__(self, js=None, status=200, content=b""):
        self.status_code, self._js, self.content = status, js, content

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


def _abp(result):
    return _Resp({"result": result, "success": True, "error": None})


CAPTCHA = _abp({"status": 1, "key": "cap-1",
                "image": "data:image/jpeg;base64," + base64.b64encode(b"png").decode()})
NOT_HERE = _abp({"status": 4, "message": "", "key": ""})
WRONG = _abp({"status": 2, "message": "Mã xác thực không hợp lệ", "key": ""})
FOUND = _abp({"status": 1, "message": "", "key": "sess-9"})
TOKEN = _abp({"status": 1, "fileName": "x.pdf", "fileType": "text/xml", "fileToken": "tok-1"})


def _setup(monkeypatch, api, tracuu, groups=None):
    calls, tracuu = [], list(tracuu)
    pdf = _Resp(None, content=_pdf_with("So 36"))
    file_url = api.split("/api/")[0] + "/File/DownloadTempFile"

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url == f"{api}/RefreshCaptcha":
            return CAPTCHA
        if url == f"{api}/TraCuu":
            return tracuu.pop(0)
        if url == f"{api}/DownloadPdf":
            return TOKEN
        if url == file_url:
            return pdf
        raise AssertionError(f"không có route cho {method} {url}")

    def solve(img, group, min_conf=0.5):
        if groups is not None:
            groups.append(group)
        return "12345"
    monkeypatch.setattr(xcyber, "request", fake)
    monkeypatch.setattr(xcyber, "new_session", lambda: object())
    monkeypatch.setattr(xcyber, "solve_captcha", solve)
    return calls


INV = {"shdon": 36, "khhdon": "C26TLS", "khmshdon": "1", "nbmst": "0301234567",
       "ma_tra_cuu": "2MG82ARHG0M6", "tt_khac": {"MaTraCuu": "2MG82ARHG0M6"}}

CASES = [
    (lienson.LienSonAdapter, "lienson", "0301452923", "lienson.vn", LS_API, "https://tracuu.lienson.vn"),
    (myinvoicevn.MyInvoiceVnAdapter, "myinvoicevn", "0106820789", "hoadondientuvn.info", B2_API,
     "https://tracuu.hoadondientuvn.info"),
]


@pytest.mark.parametrize("cls, sid, mst, host, api, site", CASES)
def test_registry(cls, sid, mst, host, api, site):
    assert isinstance(cls(), xcyber.XcyberAdapter)
    assert pdf_providers.adapter_for_mst_tcgp(mst).source_id == sid
    assert pdf_providers.adapter_for_host("tracuu." + host).source_id == sid
    assert pdf_providers.adapter_for_mst_tcgp("0105232093").source_id == "xcyber"


@pytest.mark.parametrize("cls, sid, mst, host, api, site", CASES)
def test_ok_only_own_api(monkeypatch, cls, sid, mst, host, api, site):
    groups = []
    calls = _setup(monkeypatch, api, [FOUND], groups)
    assert cls().fetch_pdf(INV).startswith(b"%PDF")
    assert groups == ["g_300x75"]
    tra = [c for c in calls if c[1].endswith("/TraCuu")]
    assert len(tra) == 1 and '"maSoBiMat": "2MG82ARHG0M6"' in tra[0][2]["data"]
    assert tra[0][2]["headers"]["Origin"] == site


@pytest.mark.parametrize("cls, sid, mst, host, api, site", CASES)
def test_not_found(monkeypatch, cls, sid, mst, host, api, site):
    calls = _setup(monkeypatch, api, [NOT_HERE])
    with pytest.raises(PdfFetchError) as e:
        cls().fetch_pdf(INV)
    assert e.value.kind == "no_pdf"
    assert not any("bill1app" in c[1] for c in calls)      # chỉ 1 hệ thống


@pytest.mark.parametrize("cls, sid, mst, host, api, site", CASES)
def test_wrong_captcha(monkeypatch, cls, sid, mst, host, api, site):
    _setup(monkeypatch, api, [WRONG] * xcyber.CAPTCHA_TRIES)
    with pytest.raises(PdfFetchError) as e:
        cls().fetch_pdf(INV)
    assert e.value.kind == "captcha"


@pytest.mark.parametrize("cls, sid, mst, host, api, site", CASES)
def test_no_code(monkeypatch, cls, sid, mst, host, api, site):
    _setup(monkeypatch, api, [])
    with pytest.raises(PdfFetchError) as e:
        cls().fetch_pdf({**INV, "ma_tra_cuu": None, "tt_khac": {}})
    assert e.value.kind == "no_code"


@pytest.mark.parametrize("cls, sid, mst, host, api, site", CASES)
def test_invoice_from_link(cls, sid, mst, host, api, site):
    a = cls()
    got = a.invoice_from_link(f"https://tracuu.{host}/#/tracuuhoadon/tracuu", "2MG82ARHG0M6", "")
    assert got["mst_tcgp"] == mst and got["ma_tra_cuu"] == "2MG82ARHG0M6"
    assert a.invoice_from_link(f"https://tracuu.{host}/", "short", "") is None
