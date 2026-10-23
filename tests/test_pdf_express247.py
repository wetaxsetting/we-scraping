"""Adapter 247Express / hoadon.247express.vn (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm 23/10/2026: 8/8 HĐ thật của công ty (người bán 0304043037, hthdon=2) tải được PDF
bằng mã trong mail, và mail_ingest khớp đúng 8/8 HĐ theo chữ trong PDF.

Run::

    pytest tests/test_pdf_express247.py -q
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
from pdf_providers import express247       # noqa: E402

CODE = "OWJQPNIJU4GAAUEMGH53RL1X"
PDF_URL = express247.PDF_URL.format(code=CODE)


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
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content = status, content
        self.headers = headers or {}


def _setup(monkeypatch, resp, calls):
    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url == PDF_URL:
            return resp
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(express247, "request", fake)
    monkeypatch.setattr(express247, "new_session", lambda: object())


# ---------------------------------------------------------------------------
# Link trong mail → dict HĐ (không mở link)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,code", [
    (f"https://hoadon.247express.vn/service/api/Invoice/DownloadFilePDF/{CODE}", None),
    (f"https://hoadon.247express.vn/tra-cuu/{CODE}", None),
    ("https://hoadon.247express.vn/", CODE),          # link trang chủ: mã lấy từ nội dung mail
    ("https://247express.vn", CODE.lower()),
])
def test_invoice_from_link(url, code):
    inv = express247.Express247Adapter().invoice_from_link(url, code, "Hóa đơn điện tử số 00464695")
    assert inv == {"ma_tra_cuu": CODE, "tt_khac": {"Mã tra cứu": CODE}}


@pytest.mark.parametrize("url,code", [
    ("https://hoadon.247express.vn/", None),
    ("https://247express.vn/lien-he", "ABC"),
])
def test_invoice_from_link_khong_co_ma(url, code):
    assert express247.Express247Adapter().invoice_from_link(url, code, "") is None


def test_host_dang_ky():
    assert pdf_providers.adapter_for_host("hoadon.247express.vn").source_id == "express247"
    assert pdf_providers.adapter_for_host("247express.vn").source_id == "express247"


# ---------------------------------------------------------------------------
# fetch_pdf
# ---------------------------------------------------------------------------

def test_fetch_pdf_ok(monkeypatch):
    calls = []
    pdf = _pdf_with("So: 00464695 Ma tra cuu " + CODE)
    _setup(monkeypatch, _Resp(content=pdf, headers={"Content-Type": "application/pdf"}), calls)
    out = express247.Express247Adapter().fetch_pdf({"ma_tra_cuu": CODE, "shdon": 464695})
    assert out == pdf
    assert [c[1] for c in calls] == [PDF_URL]


def test_fetch_pdf_khong_tim_thay(monkeypatch):
    _setup(monkeypatch, _Resp(status=404, content=b'"404"', headers={"Content-Type": "application/json"}), [])
    with pytest.raises(PdfFetchError) as e:
        express247.Express247Adapter().fetch_pdf({"ma_tra_cuu": CODE, "shdon": 464695})
    assert e.value.kind == "no_pdf"


def test_fetch_pdf_khong_phai_pdf(monkeypatch):
    _setup(monkeypatch, _Resp(content=b"<html>loi</html>", headers={"Content-Type": "text/html"}), [])
    with pytest.raises(PdfFetchError) as e:
        express247.Express247Adapter().fetch_pdf({"ma_tra_cuu": CODE, "shdon": 464695})
    assert e.value.kind == "bad_content"


def test_fetch_pdf_sai_so_hd(monkeypatch):
    pdf = _pdf_with("So: 00398293")
    _setup(monkeypatch, _Resp(content=pdf, headers={"Content-Type": "application/pdf"}), [])
    with pytest.raises(PdfFetchError) as e:
        express247.Express247Adapter().fetch_pdf({"ma_tra_cuu": CODE, "shdon": 464695})
    assert e.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [{}, {"ma_tra_cuu": "abc"}])
def test_fetch_pdf_khong_co_ma(inv):
    with pytest.raises(PdfFetchError) as e:
        express247.Express247Adapter().fetch_pdf({**inv, "shdon": 1})
    assert e.value.kind == "no_code"
