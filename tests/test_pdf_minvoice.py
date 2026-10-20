"""Adapter M-invoice (tracuuhoadon.minvoice.com.vn), HTTP giả lập — luồng thật đã kiểm 10/2026.

Run::

    pytest tests/test_pdf_minvoice.py -q
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pdf_providers                         # noqa: E402
from pdf_providers import PdfFetchError      # noqa: E402
from pdf_providers import common, minvoice   # noqa: E402


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
    def __init__(self, result):
        self.result, self.calls = result, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _use(monkeypatch, result) -> _Sess:
    sess = _Sess(result)
    monkeypatch.setattr(minvoice, "new_session", lambda: sess)
    return sess


INV = {"shdon": 11001, "khhdon": "C26TCA", "khmshdon": 1, "nbmst": "0104957749-001",
       "mst_tcgp": "0106026495", "ngay_lap": date(2026, 9, 15), "tong_tien": Decimal("2500000"),
       "tt_khac": {"Số bảo mật": "74AF34BF16B14504"}}
INV_MTT = {"shdon": 2703, "khhdon": "C26MBH", "khmshdon": "1", "nbmst": "0110097153",
           "mst_tcgp": "0106026495-001", "ngay_lap": "2026-09-22", "tong_tien": "1000",
           "loai_hd": "MTT",
           "tt_khac": {"Tỷ giá": "1", "Mã tra cứu": "DCCE75596C062723", "Địa chỉ": "x"}}
NOT_FOUND = {"code": "5000", "message": "Thông tin tra cứu không chính xác, vui lòng kiểm tra "
                                        "lại Mã số thuế và Số bảo mật.\n", "data": {"Code": 5000}}


def test_registry_maps_parent_and_branch():
    assert pdf_providers.adapter_for_mst_tcgp("0106026495").source_id == "minvoice"
    assert pdf_providers.adapter_for_mst_tcgp("0106026495-001").source_id == "minvoice"


def test_ok_sends_seller_mst_and_security_code(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 11001")))
    assert minvoice.MinvoiceAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "GET" and url.endswith("/api/Search/SearchInvoice")
    assert kw["params"] == {"masothue": "0104957749-001", "sobaomat": "74AF34BF16B14504",
                            "type": "PDF", "inchuyendoi": "false"}


def test_mtt_uses_ma_tra_cuu(monkeypatch):
    sess = _use(monkeypatch, _Resp(content=_pdf_with("So 2703")))
    assert minvoice.MinvoiceAdapter().fetch_pdf(dict(INV_MTT)).startswith(b"%PDF")
    assert sess.calls[0][2]["params"]["sobaomat"] == "DCCE75596C062723"


@pytest.mark.parametrize("inv", [
    {"shdon": 1, "nbmst": "0104957749", "tt_khac": {}},
    {"shdon": 1, "nbmst": "", "tt_khac": {"Số bảo mật": "ABC"}},
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


@pytest.mark.parametrize("result, kind", [
    (_Resp(500, b"{}", NOT_FOUND, "application/json"), "no_pdf"),     # 500 nhưng là "không thấy"
    (_Resp(500, b"<html>loi</html>", None, "text/html"), "server"),
    (_Resp(502, b"{}", {"message": "Bad gateway"}, "application/json"), "server"),
    (_Resp(429, b""), "rate_limit"),
    (_Resp(400, b"{}", {"title": "validation"}, "application/problem+json"), "other"),
    (requests.Timeout("t"), "network"),
    (requests.ConnectionError("c"), "network"),
    (_Resp(200, b"<html>" + b"x" * 2000, None, "text/html"), "bad_content"),
    (_Resp(200, _pdf_with("So 99999")), "bad_content"),                 # PDF của HĐ khác
])
def test_errors(monkeypatch, result, kind):
    _use(monkeypatch, result)
    with pytest.raises(PdfFetchError) as ei:
        minvoice.MinvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind
