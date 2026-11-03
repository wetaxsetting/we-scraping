"""Adapter VN-Invoice vninvoice.vn (HTTP + model captcha giả lập, không cần mạng).

Luồng thật đã kiểm 02/11/2026: 15/15 HĐ trong DB (FamilyMart 0312283473, Phong Vũ 0304998358,
1800457284) → PDF + XML, mã tra cứu = TTKhac "Mã bảo mật".

Run::

    pytest tests/test_pdf_vninvoice.py -q
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError, get_adapter   # noqa: E402
from pdf_providers import common                       # noqa: E402
from pdf_providers import vninvoice                    # noqa: E402

INV = {"nbmst": "0312283473", "khmshdon": "1", "khhdon": "K23TAN", "shdon": 278167,
       "ngay_lap": "2023-11-08", "tt_khac": {"Mã bảo mật": "2023110290FwB0JnUm05"}}
ITEM = {"id": 36936267, "templateNo": 1, "serialNo": "K23TAN", "invoiceNo": "00278167", "number": 278167,
        "vnisType": 1, "sellerTaxCode": "0312283473", "transactionId": "2023110290FwB0JnUm05"}


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):
        page.insert_text((40, 40 + i * 18), f"{text} dong {chr(97 + i % 26)}")
    data = doc.tobytes()
    doc.close()
    return data


class _Resp:
    def __init__(self, json=None, status=200):
        self._json, self.status_code, self.text = json, status, str(json)

    def json(self):
        return self._json


class _Session:
    def __init__(self, routes, calls):
        self.routes, self.calls = routes, calls

    def request(self, method, url, **kw):
        self.calls.append((method, url.rsplit("/PortalApiService/", 1)[1], kw.get("json")))
        return self.routes(method, url.rsplit("/PortalApiService/", 1)[1], kw.get("json"))


def _setup(monkeypatch, answers, caps=("Ab3dE9",), pdf=None, xml=b"<HDon><SHDon>278167</SHDon></HDon>"):
    """answers: kết quả POST portal-public lần lượt: "ok" | "empty" | "captcha" | "notfound"."""
    calls, answers, caps = [], list(answers), list(caps)
    pdf = pdf if pdf is not None else _pdf_with("K23TAN So 278167")

    def routes(method, path, body):
        if path == "captcha":
            return _Resp({"image": "data:image/png;base64," + base64.b64encode(b"img").decode(), "key": "k1"})
        if path == "portal-public":
            a = answers.pop(0)
            if a == "ok":
                return _Resp([ITEM])
            if a == "empty":
                return _Resp([])
            msg = {"captcha": "Mã xác thực không đúng", "notfound": "Mã số thuế người bán không tìm thấy"}[a]
            return _Resp({"error": {"message": msg}}, 500)
        if path == "download/public/unofficial":
            return _Resp({"fileBytes": base64.b64encode(pdf).decode()})
        if path == "download/public/xml":
            return _Resp({"fileBytes": base64.b64encode(xml).decode()})
        raise AssertionError(path)

    monkeypatch.setattr(vninvoice, "new_session", lambda: _Session(routes, calls))
    monkeypatch.setattr(vninvoice, "solve_captcha", lambda img, group: caps.pop(0) if len(caps) > 1 else caps[0])
    return calls


def test_ok_pdf_and_xml_one_captcha(monkeypatch):
    calls = _setup(monkeypatch, ["ok"])
    a = vninvoice.VnInvoiceAdapter()
    assert common.is_pdf(a.fetch_pdf(dict(INV)))
    assert b"278167" in a.fetch_xml(dict(INV))
    posts = [b for m, p, b in calls if p == "portal-public"]
    assert len(posts) == 1                                   # XML dùng lại lần tra, không giải captcha 2 lần
    assert posts[0] == {"transactionId": "2023110290FwB0JnUm05", "captcha": "Ab3dE9", "secret": "k1",
                        "taxCode": "0312283473", "fromDate": "2023-11-08", "toDate": "2023-11-08"}


def test_skips_short_answer_and_retries_wrong_captcha(monkeypatch):
    calls = _setup(monkeypatch, ["captcha", "ok"], caps=["Ab3dE", "Zz9yX8", "Ab3dE9"])
    vninvoice.VnInvoiceAdapter().fetch_pdf(dict(INV))
    assert [b["captcha"] for m, p, b in calls if p == "portal-public"] == ["Zz9yX8", "Ab3dE9"]


def test_captcha_exhausted(monkeypatch):
    _setup(monkeypatch, ["captcha"] * vninvoice.MAX_TRIES)
    with pytest.raises(PdfFetchError) as ei:
        vninvoice.VnInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "captcha"


@pytest.mark.parametrize("answer", ["empty", "notfound"])
def test_not_found(monkeypatch, answer):
    _setup(monkeypatch, [answer])
    with pytest.raises(PdfFetchError) as ei:
        vninvoice.VnInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "no_pdf"


def test_pdf_of_other_invoice(monkeypatch):
    _setup(monkeypatch, ["ok"], pdf=_pdf_with("C23TSG So 999"))
    with pytest.raises(PdfFetchError) as ei:
        vninvoice.VnInvoiceAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == "bad_content"


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        vninvoice.VnInvoiceAdapter().fetch_pdf({**INV, "tt_khac": {"PONumber": "1"}})
    assert ei.value.kind == "no_code"


def test_code_key_variants():
    assert vninvoice.VnInvoiceAdapter.lookup_code({"tt_khac": {"Số bảo mật": "1142a6d5"}}) == "1142a6d5"
    assert vninvoice.VnInvoiceAdapter.lookup_code({"tt_khac": {"Mã tra cứu": "abc123"}}) == "abc123"


def test_registered():
    a = get_adapter("vninvoice")
    assert a is not None and a.mst_tcgp == ("0101352495", "0103018807")
