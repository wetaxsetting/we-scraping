"""Adapter EasyInvoice (Softdreams), HTTP giả lập.

Tập trung vào:
  * invoice_from_link: giải mã token, kể cả Fkey có dấu '-' của cổng riêng
    (8404421833-001, 8530810438-002) — regex cũ cắt mất đuôi Fkey.
  * portals(): cổng chung + cổng riêng subdomain người bán.
  * luồng fetch_pdf: Search → Captcha → Search(POST) → DownloadPdf → Download(ZIP).

Run::

    pytest tests/test_pdf_easyinvoice.py -q
"""

from __future__ import annotations

import base64
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                                   # noqa: E402
from pdf_providers import PdfFetchError               # noqa: E402
from pdf_providers import common, easyinvoice         # noqa: E402


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(30):
        page.insert_text((40, 40 + i * 18), f"{text} dong {i}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


def _zip_with(pdf: bytes, name="hoadon.pdf") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, pdf)
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, content=b"", text=None, js=None):
        self.status_code = status
        self.content = content
        self._text = text
        self._js = js

    @property
    def text(self):
        if self._text is not None:
            return self._text
        return self.content.decode("utf-8", "replace")

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


# --------------------------------------------------------------------------
# invoice_from_link
# --------------------------------------------------------------------------
def test_registry_host():
    a = pdf_providers.adapter_for_host("0104947250001hd.easyinvoice.com.vn")
    assert a.source_id == "easyinvoice"
    assert pdf_providers.adapter_for_mst_tcgp("0105987432").source_id == "easyinvoice"


def test_link_fkey_with_hyphens():
    """Cổng riêng 8404421833-001: Fkey = M2-24-DA8RU-17161791187 (có '-')."""
    url = ("http://8404421833001hd.easyinvoice.com.vn/Invoice/DownloadInvPdf?"
           "token=MkMyNE1BQV9fMzQyfE0yLTI0LURBOFJVLTE3MTYxNzkxMTg3")
    inv = easyinvoice.EasyInvoiceAdapter().invoice_from_link(url, None, "")
    assert inv["nbmst"] == "8404421833-001"
    assert inv["khmshdon"] == 2 and inv["khhdon"] == "C24MAA" and inv["shdon"] == 342
    assert inv["ma_tra_cuu"] == "M2-24-DA8RU-17161791187"      # đuôi không bị cắt
    assert inv["tt_khac"]["Fkey"] == "M2-24-DA8RU-17161791187"
    assert inv["lookup_url"] == "http://8404421833001hd.easyinvoice.com.vn"


def test_link_fkey_plain():
    token = base64.b64encode(b"1C26TAL__37|RZAPJM7MQ").decode()
    url = f"http://0104947250001hd.easyinvoice.com.vn/Invoice/DownloadInvPdf?token={token}"
    inv = easyinvoice.EasyInvoiceAdapter().invoice_from_link(url, None, "")
    assert inv["ma_tra_cuu"] == "RZAPJM7MQ"
    assert inv["nbmst"] == "0104947250-001"


def test_link_bad_token():
    url = "http://0104947250001hd.easyinvoice.com.vn/Invoice/DownloadInvPdf?token=not%20base64!"
    assert easyinvoice.EasyInvoiceAdapter().invoice_from_link(url, None, "") is None


def test_portals_order():
    inv = {"nbmst": "8404421833-001", "tt_khac": {},
           "lookup_url": "http://8404421833001hd.easyinvoice.com.vn"}
    out = easyinvoice.EasyInvoiceAdapter.portals(inv)
    assert out[0] == (easyinvoice.GENERIC_BASE, True)
    assert out[1] == ("http://8404421833001hd.easyinvoice.com.vn", False)


# --------------------------------------------------------------------------
# fetch_pdf (luồng đầy đủ, cổng chung tìm thấy ngay)
# --------------------------------------------------------------------------
def _found_page(token="TKN1", shdon=2087):
    inv_json = json.dumps({"str": f"<div>Hoa don so {shdon}</div>"}).replace('"', "&quot;")
    return (f'<input type="hidden" id="msg" value="" />'
            f'<input type="hidden" id="InvData" value="{inv_json}" />'
            f"<script>showInv(data.str, obj, '{token}');</script>")


def test_fetch_ok(monkeypatch):
    monkeypatch.setattr(easyinvoice, "solve_captcha", lambda *a, **k: "0000")
    pdf = _pdf_with("Hoa don so 2087")
    sess = _Sess(
        _Resp(text="<html>index</html>"),              # GET /Search/Index
        _Resp(content=b"\x89PNGcaptcha"),              # GET /Captcha/Show
        _Resp(text=_found_page()),                     # POST /Search/Search
        _Resp(js={"fileGuid": "G1", "fileName": "a.pdf"}),   # POST DownloadPdf...
        _Resp(content=_zip_with(pdf)),                 # GET /Invoice/Download
    )
    monkeypatch.setattr(easyinvoice, "new_session", lambda: sess)
    inv = {"shdon": 2087, "nbmst": "0105844836", "tt_khac": {"Fkey": "RZAPJM7MQ"}}
    out = easyinvoice.EasyInvoiceAdapter().fetch_pdf(inv)
    assert out.startswith(b"%PDF")
    # bước 3 là POST Search với FKey + Capcha
    post = [c for c in sess.calls if c[0] == "POST" and c[1].endswith("/Search/Search")][0]
    assert post[2]["data"]["FKey"] == "RZAPJM7MQ"
    assert post[2]["data"]["Capcha"] == "0000"


def test_fetch_no_fkey():
    with pytest.raises(PdfFetchError) as e:
        easyinvoice.EasyInvoiceAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert e.value.kind == "no_code"


def test_fetch_not_found(monkeypatch):
    monkeypatch.setattr(easyinvoice, "solve_captcha", lambda *a, **k: "0000")

    def _sess():
        return _Sess(
            _Resp(text="index"),
            _Resp(content=b"png"),
            _Resp(text='<input id="msg" value="Mã tra cứu không tồn tại" />'),
        )
    monkeypatch.setattr(easyinvoice, "new_session", _sess)
    inv = {"shdon": 1, "nbmst": "0105844836", "tt_khac": {"Fkey": "ZZZ"}}
    with pytest.raises(PdfFetchError) as e:
        easyinvoice.EasyInvoiceAdapter().fetch_pdf(inv)
    assert e.value.kind == "no_pdf"
