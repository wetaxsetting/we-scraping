"""Adapter ACMan (hoadondientu.acman.vn), HTTP giả lập — khảo sát 27/10/2026, chưa có HĐ thật.

Run::

    pytest tests/test_pdf_acman.py -q
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
    def __init__(self, status=200, content=b"", js=None, ctype="application/pdf", url=""):
        self.status_code, self.content, self._js, self.url = status, content, js, url
        self.headers = {"Content-Type": ctype}
        self.text = content.decode("utf-8", "replace")

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


def _html(s: str) -> _Resp:
    return _Resp(content=s.encode(), ctype="text/html; charset=utf-8")


from pdf_providers import acman  # noqa: E402

INV = {"shdon": 9, "nbmst": "0109999999", "mst_tcgp": "0104908371", "tt_khac": {"Mã tra cứu": "ACM9"}}
PAGE = ('<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="VS&amp;1" />'
        '<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="EV" />')


def _use(monkeypatch, *results) -> _Sess:
    sess = _Sess(*results)
    monkeypatch.setattr(acman, "new_session", lambda: sess)
    return sess


def test_registry():
    assert pdf_providers.adapter_for_mst_tcgp("0104908371").source_id == "acman"
    assert pdf_providers.adapter_for_host("hoadondientu.acman.vn").source_id == "acman"


def test_postback_and_pdf(monkeypatch):
    sess = _use(monkeypatch, _html(PAGE), _Resp(content=_pdf_with("So 9")))
    assert acman.AcmanAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    method, url, kw = sess.calls[1]
    assert method == "POST" and url == acman.URL
    assert kw["data"]["__VIEWSTATE"] == "VS&1" and kw["data"]["__EVENTVALIDATION"] == "EV"
    assert kw["data"]["__EVENTTARGET"] == "ctl00$PageContent$btnTraCuu"
    assert kw["data"]["ctl00$PageContent$txtMaDonViPhatHanh"] == "0109999999"
    assert kw["data"]["ctl00$PageContent$txtMaTraCuuHoaDon"] == "ACM9"


def test_pdf_link_in_html(monkeypatch):
    found = _Resp(content=b'<iframe src="/Files/HD_9.pdf"></iframe><a href="https://evil.example/x.pdf">x</a>',
                  ctype="text/html", url=acman.URL)
    sess = _use(monkeypatch, _html(PAGE), found, _Resp(content=_pdf_with("So 9")))
    assert acman.AcmanAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")
    assert sess.calls[2][1] == "https://hoadondientu.acman.vn/Files/HD_9.pdf"


def test_not_found(monkeypatch):
    _use(monkeypatch, _html(PAGE), _html("<span>Không tìm thấy hóa đơn, vui lòng kiểm tra thông tin</span>"))
    with pytest.raises(PdfFetchError) as ei:
        acman.AcmanAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_unknown_html(monkeypatch):
    _use(monkeypatch, _html(PAGE), _html("<html>khác</html>"))
    with pytest.raises(PdfFetchError) as ei:
        acman.AcmanAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("inv", [{"shdon": 1, "nbmst": "0109999999", "tt_khac": {}},
                                 {"shdon": 1, "nbmst": "", "ma_tra_cuu": "X"}])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        acman.AcmanAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_invoice_from_link():
    assert acman.AcmanAdapter().invoice_from_link(acman.URL, "X", "") is None
