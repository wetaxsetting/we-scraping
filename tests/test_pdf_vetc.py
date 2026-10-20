"""Adapter VETC (tracuuhoadon.vetc.com.vn), HTTP giả lập — luồng thật đã kiểm 10/2026.

Run::

    pytest tests/test_pdf_vetc.py -q
"""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common, vetc     # noqa: E402


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
    def __init__(self, status=200, content=b"", text=""):
        self.status_code, self.content, self.text, self.headers = status, content, text, {}


def _router(monkeypatch, routes, calls):
    """routes = list[(method, url_endswith|contains, resp)] — khớp theo thứ tự."""
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        for m, frag, resp in routes:
            if m == method and frag in url:
                return resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(vetc, "request", fake)
    monkeypatch.setattr(vetc, "new_session", lambda: object())


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


CODE = "EJoLM6NUHqXJ"
INV = {"shdon": 80957898, "khhdon": "K26TEF", "khmshdon": 1, "nbmst": "0107500414",
       "mst_tcgp": "0107500414", "ngay_lap": date(2026, 9, 30), "tong_tien": Decimal("1000"),
       "tt_khac": {"Mã tra cứu": CODE}}

# JS của trang luôn nhắc tới secureIdError2 → không được coi đó là dấu hiệu "không thấy"
_JS = '<script>document.getElementById("secureIdError2");</script>'
FOUND = _JS + (f'<td style="color: red">80957898</td><a href="/p/{CODE}">Xem</a>'
               f'<a href="/download/{CODE}" target="_blank">Tải về</a>')
NOT_FOUND = _JS + ('<div class="input-group" id="secureIdError2" style="color: red">'
                   'Không tìm thấy hóa đơn, xin vui lòng kiểm tra lại mã tra cứu của bạn!</div>')
ZIP_OK = _zip({"ATIS_80957898.xml": "<HDon/>", "ATIS_80957898.html": "<html/>",
               "ATIS_80957898.pdf": _pdf_with("So 80957898")})


def _routes(search, download=None):
    return [("GET", "/download/", download or _Resp(content=ZIP_OK)),
            ("GET", "vetc.com.vn/", _Resp()),
            ("POST", "vetc.com.vn/", search)]


def test_registry_maps_both_mst_tcgp():
    assert pdf_providers.adapter_for_mst_tcgp("0107500414").source_id == "vetc"
    assert pdf_providers.adapter_for_mst_tcgp("0106858609").source_id == "vetc"


def test_ok_downloads_pdf_from_zip(monkeypatch):
    calls = []
    _router(monkeypatch, _routes(_Resp(text=FOUND)), calls)
    pdf = vetc.VetcAdapter().fetch_pdf(dict(INV))
    assert pdf.startswith(b"%PDF")
    post = next(kw for m, _, kw in calls if m == "POST")
    assert post["data"]["searchType"] == "SECURE_ID"
    assert post["data"]["secureId"] == CODE                 # giữ nguyên hoa/thường
    assert calls[-1][1].endswith(f"/download/{CODE}")


def test_code_falls_back_to_ma_tra_cuu(monkeypatch):
    calls = []
    _router(monkeypatch, _routes(_Resp(text=FOUND)), calls)
    inv = dict(INV, tt_khac={}, ma_tra_cuu=CODE)
    assert vetc.VetcAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_without_code():
    with pytest.raises(PdfFetchError) as ei:
        vetc.VetcAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert ei.value.kind == "no_code"


@pytest.mark.parametrize("mst_tcgp, kind", [
    ("0107500414", "no_pdf"),        # HĐ cước đường bộ: có thể chưa lên cổng → thử lại
    ("0106858609", "no_portal"),     # HĐ bãi đỗ: không có trên cổng này → nằm chờ
])
def test_not_found(monkeypatch, mst_tcgp, kind):
    _router(monkeypatch, _routes(_Resp(text=NOT_FOUND)), [])
    with pytest.raises(PdfFetchError) as ei:
        vetc.VetcAdapter().fetch_pdf(dict(INV, mst_tcgp=mst_tcgp))
    assert ei.value.kind == kind


@pytest.mark.parametrize("search, download, kind", [
    (_Resp(text=_JS + "<html>trang lạ</html>"), None, "bad_content"),
    (_Resp(status=403), None, "other"),
    (_Resp(text=FOUND), _Resp(content=b"<html>" + b"x" * 2000), "bad_content"),
    (_Resp(text=FOUND), _Resp(content=_zip({"a.xml": "<x/>"})), "no_pdf"),
    (_Resp(text=FOUND), _Resp(content=_zip({"a.pdf": b"<html>loi</html>" * 200})), "bad_content"),
    (_Resp(text=FOUND), _Resp(content=_zip({"a.pdf": _pdf_with("So 11111111")})), "bad_content"),
])
def test_errors(monkeypatch, search, download, kind):
    _router(monkeypatch, _routes(search, download), [])
    with pytest.raises(PdfFetchError) as ei:
        vetc.VetcAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind
