"""Adapter Megabiz (vinvoice), HTTP giả lập.

Run::

    pytest tests/test_pdf_megabiz.py -q
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

import pdf_providers                                   # noqa: E402
from pdf_providers import PdfFetchError               # noqa: E402
from pdf_providers import common, megabiz             # noqa: E402

INV = {"shdon": 2087, "nbmst": "0105844836", "tt_khac": {"MaTraCuu": "ABC123XYZ"},
       "ma_tra_cuu": "ABC123XYZ"}


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


def _zip(pdf: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hoadon.pdf", pdf)
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, content=b"", text=None):
        self.status_code, self.content, self._text = status, content, text
        self.headers = {"Content-Type": "application/zip"}

    @property
    def text(self):
        return self._text if self._text is not None else self.content.decode("utf-8", "replace")


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.results.pop(0)


def _patch(monkeypatch, *sessions):
    it = iter(sessions)
    monkeypatch.setattr(megabiz, "new_session", lambda: next(it))
    monkeypatch.setattr(megabiz, "solve_captcha", lambda *a, **k: "0000")


# captcha đúng → ô key giữ lại + nút tải; captcha sai → ô key rỗng
FOUND = ("<input id='key' value=\"ABC123XYZ\" />"
         "<a onclick=\"downloadTaxInv('777')\">Tải</a>")
NOT_FOUND = "<input id='key' value=\"ABC123XYZ\" />"       # key giữ lại, không có nút tải
WRONG = "<input id='key' value=\"\" />"                     # key bị xóa


def test_registry_host():
    assert pdf_providers.adapter_for_mst_tcgp("0105844836").source_id == "megabiz"
    assert pdf_providers.adapter_for_host("tracuu.vinvoice.vn").source_id == "megabiz"


def test_fetch_ok(monkeypatch):
    s = _Sess(_Resp(text="form"), _Resp(content=b"\x89PNG"), _Resp(text=FOUND),
              _Resp(content=_zip(_pdf_with("Hoa don so 2087"))))
    _patch(monkeypatch, s)
    assert megabiz.MegabizAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    dl = [c for c in s.calls if "Download" in c[1]][0]
    assert dl[1].endswith("/Invoice/DownloadTaxInv")
    assert dl[2]["params"] == {"id": "777"}


def test_not_found(monkeypatch):
    s = _Sess(_Resp(text="form"), _Resp(content=b"png"), _Resp(text=NOT_FOUND))
    _patch(monkeypatch, s)
    with pytest.raises(PdfFetchError) as e:
        megabiz.MegabizAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_wrong_captcha_then_ok(monkeypatch):
    bad = _Sess(_Resp(text="form"), _Resp(content=b"png"), _Resp(text=WRONG))
    good = _Sess(_Resp(text="form"), _Resp(content=b"png"), _Resp(text=FOUND),
                 _Resp(content=_zip(_pdf_with("Hoa don so 2087"))))
    _patch(monkeypatch, bad, good)
    assert megabiz.MegabizAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")


def test_no_code():
    with pytest.raises(PdfFetchError) as e:
        megabiz.MegabizAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert e.value.kind == "no_code"


def test_invoice_from_link():
    inv = megabiz.MegabizAdapter().invoice_from_link("https://tracuu.vinvoice.vn/?key=MA9", None, "")
    assert inv["ma_tra_cuu"] == "MA9"
    assert megabiz.MegabizAdapter().invoice_from_link("https://other.vn/?key=X", None, "") is None
