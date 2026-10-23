"""Adapter THACO (hoadondientu.thaco.com.vn), HTTP giả lập — luồng thật đã kiểm 23/10/2026.

Run::

    pytest tests/test_pdf_thaco.py -q
"""

from __future__ import annotations

import base64
import io
import sys
import zipfile
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError      # noqa: E402
from pdf_providers import common, thaco      # noqa: E402


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


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, content=b"", ctype="application/zip", headers=None):
        self.status_code, self.content = status, content
        self.headers = {"Content-Type": ctype, **(headers or {})}
        self.text = content.decode("utf-8", "replace")


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
    monkeypatch.setattr(thaco, "new_session", lambda: sess)
    return sess


TOKEN = "MUMwMlRfMUMyNVRBRF8yMDh8MDMxMzU5MjExNA=="       # 1C02T_1C25TAD_208|0313592114
INV = {"shdon": 208, "khhdon": "C25TAD", "khmshdon": 1, "nbmst": "0313592114",
       "mst_tcgp": "3600252847", "ma_tra_cuu": "fq1F90638727089473529233208",
       "tt_khac": {"SearchKey": "fq1F90638727089473529233208", "CusTaxCode": "1201496252",
                   "DownloadPdf": f"hoadondientu.thaco.com.vn/Invoice/getinvoice?token={TOKEN}"}}


def test_decode_token():
    assert thaco.decode_token(TOKEN) == {"khmshdon": 1, "khhdon": "C25TAD", "shdon": 208,
                                         "nbmst": "0313592114"}
    assert thaco.decode_token("MUMwMVRfMUMyNFRRTV80MTh8MDMxMDcxMzIxOQ==")["shdon"] == 418
    assert thaco.decode_token("không-phải-token") is None


def test_ok_zip_with_pdf(monkeypatch):
    body = _zip({"10_2026_1C02T_1C25TAD_208.pdf": _pdf_with("Số (Invoice No): 208")})
    sess = _use(monkeypatch, _Resp(content=body))
    pdf = thaco.ThacoAdapter().fetch_pdf(dict(INV))
    assert pdf.startswith(b"%PDF")
    method, url, kw = sess.calls[0]
    assert method == "GET" and url == "https://hoadondientu.thaco.com.vn/Invoice/getinvoice"
    assert kw["params"] == {"token": TOKEN} and kw["allow_redirects"] is False


def test_ok_plain_pdf(monkeypatch):
    _use(monkeypatch, _Resp(content=_pdf_with("So 208"), ctype="application/pdf"))
    assert thaco.ThacoAdapter().fetch_pdf(dict(INV)).startswith(b"%PDF")


def test_no_download_link():
    inv = {**INV, "tt_khac": {"SearchKey": "fq1F90638727089473529233208"}}
    with pytest.raises(PdfFetchError) as ei:
        thaco.ThacoAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_token_of_other_invoice():
    with pytest.raises(PdfFetchError) as ei:
        thaco.ThacoAdapter().fetch_pdf({**INV, "shdon": 173})
    assert ei.value.kind == "bad_content"


@pytest.mark.parametrize("result, kind", [
    (_Resp(302, b"<html>Object moved</html>", "text/html", {"Location": "/Invoice/Index"}), "no_pdf"),
    (_Resp(500, b"loi", "text/html"), "server"),
    (_Resp(429, b""), "rate_limit"),
    (_Resp(404, b"", "text/html"), "other"),
    (requests.Timeout("t"), "network"),
    (requests.ConnectionError("c"), "network"),
    (_Resp(200, b"<html>" + b"x" * 2000, "text/html"), "bad_content"),
    (_Resp(200, b"PK\x03\x04hong"), "bad_content"),
    (_Resp(200, _zip({"readme.txt": b"x"})), "bad_content"),
    (_Resp(200, _zip({"a_208.pdf": _pdf_with("So 99999")})), "bad_content"),   # PDF của HĐ khác
])
def test_errors(monkeypatch, result, kind):
    _use(monkeypatch, result)
    with pytest.raises(PdfFetchError) as ei:
        thaco.ThacoAdapter().fetch_pdf(dict(INV))
    assert ei.value.kind == kind


def test_invoice_from_link():
    a = thaco.ThacoAdapter()
    url = f"https://hoadondientu.thaco.com.vn/Invoice/getinvoice?token={TOKEN}"
    inv = a.invoice_from_link(url, None, "")
    assert inv["shdon"] == 208 and inv["khhdon"] == "C25TAD" and inv["nbmst"] == "0313592114"
    assert a.token(inv) == TOKEN
    assert a.invoice_from_link("https://hoadondientu.thaco.com.vn/", None, "") is None
    assert a.invoice_from_link(f"https://evil.example/Invoice/getinvoice?token={TOKEN}", None, "") is None
    assert base64.b64decode(TOKEN).endswith(b"|0313592114")
