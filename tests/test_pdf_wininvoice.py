"""Adapter WinInvoice / tracuu.wininvoice.vn (HTTP giả lập, không cần mạng).

Cùng phần mềm với eVAT nên dùng lại luồng của pdf_providers/evat.py, chỉ đổi cổng.
Luồng thật đã kiểm 26/10/2026: 6/6 HĐ mua vào thật (Thiên Lý, GENUSS, Việt Uy Tín, The Only Way,
Hộp Gia Vị) tải được PDF có đúng số HĐ + MST người mua; XML trong ZIP có chữ ký.

Run::

    pytest tests/test_pdf_wininvoice.py -q
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
from pdf_providers import wininvoice       # noqa: E402

BASE = "https://tracuu.wininvoice.vn"
DL = "/bill/view?name=C25MTL_00175268&amp;f=ABC&amp;download=1"


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


def _zip(pdf=None, xml=b"<HDon><SHDon>175268</SHDon></HDon>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("C25MTL_00175268signed.pdf", pdf or _pdf_with("So 175268"))
        z.writestr("C25MTL_00175268signed.xml", xml)
    return buf.getvalue()


class _Resp:
    def __init__(self, text="", status=200, content=b"", url=BASE + "/bill", headers=None):
        self.text, self.status_code, self.content, self.url = text, status, content, url
        self.headers = headers or {}


HOME = _Resp('<form class="login-form" action="/bill" method="post"><input name="private_code"/>'
             '<input type="hidden" name="phtcsrf_tkndf-tc"  value="tok9"/>'
             '<input type="hidden" name="test" value="1" /></form>', url=BASE + "/")
FOUND = _Resp(f'<a href="{DL}">Tải</a>', url=BASE + "/bill/inv")
NO_INFO = _Resp('<b>ERROR: </b> Vui lòng nhập đầy đủ thông tin')


def _setup(monkeypatch, calls, result=FOUND):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET" and url == BASE + "/":
            return HOME
        if method == "POST" and url == BASE + "/bill":
            return result
        if url.startswith(BASE + "/bill/view?name="):
            return _Resp(content=_zip())
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(evat, "request", fake)
    monkeypatch.setattr(evat, "new_session", lambda: object())


INV = {"shdon": 175268, "khhdon": "C25MTL", "khmshdon": 1, "nbmst": "0317343246", "mst_tcgp": "0312303803",
       "tt_khac": {"Mã tra cứu hóa đơn": "535578E950", "Mã công ty": "0317343246"}}


def test_ok_uses_wininvoice_portal(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    assert wininvoice.WinInvoiceAdapter().fetch_pdf(INV).startswith(b"%PDF")
    post = [kw for m, _, kw in calls if m == "POST"][0]
    assert post["data"] == {"private_code": "535578E950", "cmpn_key": "0317343246", "action": "submit",
                            "phtcsrf_tkndf-tc": "tok9", "test": "1"}
    assert all(u.startswith(BASE) for _, u, _ in calls)


def test_fetch_xml(monkeypatch):
    _setup(monkeypatch, [])
    assert b"<SHDon>175268</SHDon>" in wininvoice.WinInvoiceAdapter().fetch_xml(INV)


def test_portal_ignores_mst_tcgp():
    a = wininvoice.WinInvoiceAdapter()
    assert a.portal({}) == "tracuu.wininvoice.vn"
    assert a.portal({"lookup_url": "http://tracuu.evat.vn"}) == "tracuu.wininvoice.vn"


def test_missing_code():
    with pytest.raises(PdfFetchError) as ei:
        wininvoice.WinInvoiceAdapter().fetch_pdf(dict(INV, tt_khac={}))
    assert ei.value.kind == "no_code"


def test_server_error_message(monkeypatch):
    _setup(monkeypatch, [], result=NO_INFO)
    with pytest.raises(PdfFetchError) as ei:
        wininvoice.WinInvoiceAdapter().fetch_pdf(INV)
    assert ei.value.kind == "bad_content"


def test_invoice_from_link():
    a = wininvoice.WinInvoiceAdapter()
    # mail chỉ có mã, không có "Mã công ty" → không tra được (form bắt buộc)
    assert a.invoice_from_link("https://tracuu.wininvoice.vn", "535578E950", "HĐĐT: 00175268 - THIÊN LÝ") is None
    assert a.invoice_from_link("https://wininvoice.vn/bghddt.php", "535578E950", "") is None
    inv = a.invoice_from_link("http://tracuu.evat.vn", "742800EC42", "Mã công ty: 0311767270")
    assert inv["nbmst"] == "0311767270" and inv["tt_khac"]["Mã tra cứu hóa đơn"] == "742800EC42"
    assert a.invoice_from_link("https://phuongnam.evat.vn", "742800EC42", "Mã công ty: 0311767270") is None


def test_declares():
    a = wininvoice.WinInvoiceAdapter()
    assert a.source_id == "wininvoice" and a.mst_tcgp == ("0312303803",)
    assert set(a.hosts) == {"wininvoice.vn", "tracuu.evat.vn"}
    assert set(a.hosts).isdisjoint(evat.EvatAdapter.hosts)
