"""Adapter BKAV eHoadon (pdf_providers/bkav.py) — không cần mạng.

Run::

    pytest tests/test_pdf_bkav.py -q
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
from pdf_providers import bkav, common     # noqa: E402

GUID = "70611f78-501c-4fce-a2dc-b31dd272f873"
INV = {"shdon": 609, "khhdon": "C26TGN", "khmshdon": 1, "nbmst": "0315908067",
       "mst_tcgp": "0101360697", "ma_tra_cuu": None, "tt_khac": {},
       "ma_cqt": "004351E7EF0344471496D74FBD05ED0597", "dlhdon_id": GUID}

PAGE_FILE = ('<form method="post" action="./Lookup?InvoiceGUID=x&amp;ViewATF=" id="formMain">'
             '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="VS1" />'
             '<li>- MTC: <b>SXUISHMLFA7</b></li>'
             '<a id="LinkDownPDF" onclick="DownloadFile(&#39;C2/6T/C26TGN-00000609-SXUISHMLFA7-DPH.pdf&#39;,1);">'
             'PDF</a></form>')
PAGE_POSTBACK = ('<form method="post" action="./Lookup?InvoiceGUID=x&amp;ViewATF=" id="formMain">'
                 '<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />'
                 '<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="VS&amp;2" />'
                 '<input type="hidden" name="UcHtmlPdfToolbar$txtIsToolbarDisplay" value="True" />'
                 '<input type="image" name="btnDownload" src="/img/Download4.png" />'
                 '<a id="LinkDownPDF" onclick="javascript:__doPostBack(&#39;lbtDownloadAsPdf&#39;,&#39;&#39;);">'
                 'PDF</a></form>')
PAGE_NOT_FOUND = ("<script>bkav_alert_error('Hóa đơn không tồn tại trên hệ thống "
                  "[!|639275098081585717|!]', 800);</script>")
PAGE_OTHER_ALERT = "<script>bkav_alert_error('Hệ thống đang bảo trì [!|1|!]', 800);</script>"


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
    def __init__(self, status=200, content=b"", text="", headers=None, url=""):
        self.status_code, self.content, self.text = status, content, text
        self.headers, self.url = headers or {}, url


def _install(monkeypatch, page, pdf, calls, dl_status=200):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url.endswith("/Lookup") and method == "GET":
            return _Resp(text=page, url=f"{url}?InvoiceGUID={kw['params']['InvoiceGUID']}&ViewATF=")
        if "/Lookup?InvoiceGUID=" in url and method == "POST":
            return _Resp(status=dl_status, content=pdf, headers={"Content-Type": "application/pdf"})
        if url.endswith("/DownloadFile"):
            return _Resp(status=dl_status, content=pdf, headers={"Content-Type": "application/pdf"})
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(bkav, "request", fake)
    monkeypatch.setattr(bkav, "new_session", lambda: object())


# --- đăng ký / mã ------------------------------------------------------------

def test_registered_and_auto_matched_by_mst_tcgp():
    assert pdf_providers.get_adapter("bkav") is not None
    assert pdf_providers.adapter_for_mst_tcgp("0101360697").source_id == "bkav"


@pytest.mark.parametrize("inv, guid", [
    ({"dlhdon_id": GUID}, GUID),
    ({"dlhdon_id": f"  {GUID.upper()} "}, GUID.upper()),
    ({"tt_khac": {"InvoiceGUID": GUID}}, GUID),
    ({"dlhdon_id": "DL"}, ""),                                # Id không phải GUID (NCC khác)
    ({"dlhdon_id": None, "tt_khac": None}, ""),
])
def test_invoice_guid(inv, guid):
    assert bkav.BkavAdapter.invoice_guid(inv) == guid


def test_no_guid_is_no_code_without_network(monkeypatch):
    monkeypatch.setattr(bkav, "request", lambda *a, **k: pytest.fail("không được gọi mạng"))
    with pytest.raises(PdfFetchError) as e:
        bkav.BkavAdapter().fetch_pdf({**INV, "dlhdon_id": None})
    assert e.value.kind == "no_code"


# --- tải PDF -----------------------------------------------------------------

def test_download_file_variant(monkeypatch):
    calls = []
    pdf = _pdf_with("C26TGN So 00000609")
    _install(monkeypatch, PAGE_FILE, pdf, calls)
    assert bkav.BkavAdapter().fetch_pdf(dict(INV)) == pdf
    (m1, u1, k1), (m2, u2, k2) = calls
    assert (m1, k1["params"]) == ("GET", {"InvoiceGUID": GUID, "ViewATF": ""})
    assert m2 == "GET" and u2.endswith("/DownloadFile")
    assert k2["params"] == {"FilePath": "C2/6T/C26TGN-00000609-SXUISHMLFA7-DPH.pdf", "BFType": "1"}


def test_postback_variant_posts_form_fields(monkeypatch):
    calls = []
    pdf = _pdf_with("C26TGN So 00000609")
    _install(monkeypatch, PAGE_POSTBACK, pdf, calls)
    assert bkav.BkavAdapter().fetch_pdf(dict(INV)) == pdf
    method, url, kw = calls[1]
    assert method == "POST" and url.endswith(f"/Lookup?InvoiceGUID={GUID}&ViewATF=")
    data = kw["data"]
    assert data["__EVENTTARGET"] == "lbtDownloadAsPdf" and data["__EVENTARGUMENT"] == ""
    assert data["__VIEWSTATE"] == "VS&2"                     # bỏ escape HTML
    assert data["UcHtmlPdfToolbar$txtIsToolbarDisplay"] == "True"
    assert "btnDownload" not in data                         # input type=image không gửi


@pytest.mark.parametrize("page, kind", [
    (PAGE_NOT_FOUND, "no_pdf"),
    ("<html>trang trống</html>", "no_pdf"),
    (PAGE_OTHER_ALERT, "other"),
])
def test_lookup_errors(monkeypatch, page, kind):
    calls = []
    _install(monkeypatch, page, b"", calls)
    with pytest.raises(PdfFetchError) as e:
        bkav.BkavAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == kind
    assert len(calls) == 1


def test_non_pdf_is_bad_content(monkeypatch):
    _install(monkeypatch, PAGE_FILE, b"<html>loi</html>", [])
    with pytest.raises(PdfFetchError) as e:
        bkav.BkavAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "bad_content"


def test_download_http_error_is_no_pdf(monkeypatch):
    _install(monkeypatch, PAGE_FILE, b"", [], dl_status=404)
    with pytest.raises(PdfFetchError) as e:
        bkav.BkavAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_pdf_of_other_invoice_is_rejected(monkeypatch):
    _install(monkeypatch, PAGE_POSTBACK, _pdf_with("C26TGN So 00000777"), [])
    with pytest.raises(PdfFetchError) as e:
        bkav.BkavAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "bad_content"


def test_db_row_types(monkeypatch):
    """Dòng từ DB: khmshdon int, tong_tien Decimal, ngay_lap date, tt_khac None."""
    from datetime import date
    from decimal import Decimal
    pdf = _pdf_with("C26TGN So 00000609")
    _install(monkeypatch, PAGE_FILE, pdf, [])
    row = {**INV, "tt_khac": None, "tong_tien": Decimal("4320000"), "ngay_lap": date(2026, 9, 30)}
    assert bkav.BkavAdapter().fetch_pdf(row) == pdf
