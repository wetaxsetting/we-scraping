"""Adapter ATIS (tracuuhoadon.atis.com.vn, cùng phần mềm cổng VETC), HTTP giả lập.

Run::

    pytest tests/test_pdf_atis.py -q
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
from pdf_providers import PdfFetchError                # noqa: E402
from pdf_providers import atis, common                 # noqa: E402


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
    def __init__(self, status=200, content=b"", text=None):
        if text is not None:
            content = text.encode("utf-8")
        self.status_code, self.content = status, content
        self.text = content.decode("utf-8", "replace")
        self.headers = {}


class _Sess:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.results.pop(0)


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(atis, "new_session", lambda: sess)
    return sess


CODE = "AbCdEf12345"
INV = {"shdon": 2963810, "tt_khac": {"Mã tra cứu": CODE}}
NOT_FOUND = ('<div class="input-group" id="secureIdError2" style="color: red">'
             "Không tìm thấy hóa đơn, xin vui lòng kiểm tra lại mã tra cứu của bạn!</div>")


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ATIS_2963810.xml", "<HDon><SHDon>2963810</SHDon></HDon>")
        zf.writestr("ATIS_2963810.pdf", _pdf_with("So 2963810"))
    return buf.getvalue()


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0107732197").source_id == "atis"
    assert pdf_providers.adapter_for_host("tracuuhoadon.atis.com.vn").source_id == "atis"


def test_ok(monkeypatch):
    sess = _use(monkeypatch, _Resp(text="home"), _Resp(text=f'<a href="/download/{CODE}">Tải</a>'),
                _Resp(content=_zip()))
    assert atis.AtisAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[1]
    assert method == "POST" and url == "https://tracuuhoadon.atis.com.vn/"
    assert kw["data"]["searchType"] == "SECURE_ID" and kw["data"]["secureId"] == CODE
    assert sess.calls[2][1] == f"https://tracuuhoadon.atis.com.vn/download/{CODE}"


def test_xml(monkeypatch):
    _use(monkeypatch, _Resp(text="home"), _Resp(text=f'<a href="/download/{CODE}">Tải</a>'), _Resp(content=_zip()))
    assert b"<SHDon>2963810" in atis.AtisAdapter().fetch_xml(dict(INV))


def test_not_found(monkeypatch):
    _use(monkeypatch, _Resp(text="home"), _Resp(text=NOT_FOUND))
    with pytest.raises(PdfFetchError) as e:
        atis.AtisAdapter().fetch_pdf(dict(INV))
    assert e.value.kind == "no_pdf"


def test_no_code():
    with pytest.raises(PdfFetchError) as e:
        atis.AtisAdapter().fetch_pdf({"shdon": 1, "tt_khac": {}})
    assert e.value.kind == "no_code"


def test_invoice_from_link():
    a = atis.AtisAdapter()
    assert a.invoice_from_link("https://tracuuhoadon.atis.com.vn/", CODE, "")["ma_tra_cuu"] == CODE
    assert a.invoice_from_link("https://tracuuhoadon.atis.com.vn/", None, "") is None
