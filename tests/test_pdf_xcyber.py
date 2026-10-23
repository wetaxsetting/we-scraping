"""Adapter CyberLotus / xcyber (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm trên HĐ thật 10/2026 (C24TGL-36 tải được PDF từ bill2app; C23TGV-125/126
nằm ở bill1app nhưng server trả "Lỗi phát sinh"); captcha g_300x75 không bị từ chối lần nào.

Run::

    pytest tests/test_pdf_xcyber.py -q
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import xcyber           # noqa: E402

API2 = "https://bill2app.xcyber.vn" + xcyber.API_PATH
API1 = "https://bill1app.xcyber.vn" + xcyber.API_PATH


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):                      # đủ dài để vượt ngưỡng is_pdf
        page.insert_text((40, 40 + i * 18), f"{text} dong {chr(97 + i % 26)}")  # không chèn số lạ
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


class _Resp:
    def __init__(self, js=None, status=200, content=b""):
        self.status_code, self._js, self.content = status, js, content

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


def _abp(result):
    return _Resp({"result": result, "success": True, "error": None})


CAPTCHA = _abp({"status": 1, "key": "cap-1",
                "image": "data:image/jpeg;base64," + base64.b64encode(b"png").decode()})
WRONG_CAPTCHA = _abp({"status": 2, "message": "Mã xác thực không hợp lệ", "key": ""})
NOT_HERE = _abp({"status": 4, "message": "", "key": ""})
SERVER_ERR = _abp({"status": 3, "message": "Lỗi phát sinh", "key": ""})
FOUND = _abp({"status": 1, "message": "", "key": "sess-9", "html": "<html/>"})
TOKEN = _abp({"status": 1, "fileName": "0000036.pdf", "fileType": "text/xml", "fileToken": "tok-1"})
EXPIRED = _abp({"status": 2, "message": "Phiên làm việc đã hết.Vui lòng thao tác lại", "fileToken": None})


def _setup(monkeypatch, calls, tracuu, download=(TOKEN,), file=None, answers=("12345",)):
    """tracuu = {api: [phản hồi TraCuu lần lượt]}; file = phản hồi DownloadTempFile."""
    tracuu = {k: list(v) for k, v in tracuu.items()}
    download, answers = list(download), list(answers)
    file = file if file is not None else _Resp(None, content=_pdf_with("So 36"))

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        for api in (API1, API2):
            if url == f"{api}/RefreshCaptcha":
                return CAPTCHA
            if url == f"{api}/TraCuu":
                return tracuu[api].pop(0)
            if url == f"{api}/DownloadPdf":
                return download.pop(0)
        if url.endswith("/File/DownloadTempFile"):
            return file
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(xcyber, "request", fake)
    monkeypatch.setattr(xcyber, "new_session", lambda: object())
    monkeypatch.setattr(xcyber, "solve_captcha",
                        lambda img, group, min_conf=0.5: answers.pop(0) if len(answers) > 1 else answers[0])


INV = {"shdon": 36, "khhdon": "C24TGL", "khmshdon": "1", "nbmst": "0317453030",
       "ma_tra_cuu": "2MG82ARHG0M6", "tt_khac": {"MaTraCuu": "2MG82ARHG0M6", "LHDon": "1"}}


def test_ok_on_system2(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {API2: [FOUND]})
    assert xcyber.XcyberAdapter().fetch_pdf(INV).startswith(b"%PDF")
    tra = [kw for _, u, kw in calls if u == f"{API2}/TraCuu"]
    assert json.loads(tra[0]["data"]) == {"key": "cap-1", "captcha": "12345", "maSoBiMat": "2MG82ARHG0M6"}
    dl = [kw for _, u, kw in calls if u == f"{API2}/DownloadPdf"]
    assert json.loads(dl[0]["data"]) == {"key": "sess-9", "maSoBiMat": "2MG82ARHG0M6"}   # khóa từ TraCuu
    f = [kw for _, u, kw in calls if u.endswith("/File/DownloadTempFile")][0]
    assert f["params"]["fileToken"] == "tok-1" and f["allow_redirects"] is False
    assert not any(u.startswith("https://bill1app") for _, u, _ in calls)


def test_captcha_group_is_forced(monkeypatch):
    seen = []
    _setup(monkeypatch, [], {API2: [FOUND]})
    monkeypatch.setattr(xcyber, "solve_captcha", lambda img, group, min_conf=0.5: seen.append(group) or "12345")
    xcyber.XcyberAdapter().fetch_pdf(INV)
    assert seen == ["g_300x75"]


def test_accepts_db_shaped_invoice(monkeypatch):
    inv = dict(INV, khmshdon=1, tong_tien=Decimal("1036800"), ngay_lap=date(2024, 1, 6))
    _setup(monkeypatch, [], {API2: [FOUND]})
    assert xcyber.XcyberAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_falls_back_to_system1(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {API2: [NOT_HERE], API1: [FOUND]})
    assert xcyber.XcyberAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert any(u == f"{API1}/DownloadPdf" for _, u, _ in calls)


def test_retries_wrong_captcha_with_new_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {API2: [WRONG_CAPTCHA, FOUND]})
    assert xcyber.XcyberAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert sum(u == f"{API2}/RefreshCaptcha" for _, u, _ in calls) == 2


def test_gives_up_after_tries(monkeypatch):
    _setup(monkeypatch, [], {API2: [WRONG_CAPTCHA] * xcyber.CAPTCHA_TRIES})
    with pytest.raises(PdfFetchError) as ei:
        xcyber.XcyberAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"


def test_does_not_send_bad_answer(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, {API2: []}, answers=("1234",))
    with pytest.raises(PdfFetchError) as ei:
        xcyber.XcyberAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"
    assert not any(u.endswith("/TraCuu") for _, u, _ in calls)


def test_not_found_anywhere(monkeypatch):
    _setup(monkeypatch, [], {API2: [NOT_HERE], API1: [NOT_HERE]})
    with pytest.raises(PdfFetchError) as ei:
        xcyber.XcyberAdapter().fetch_pdf(INV)
    assert ei.value.kind == "no_pdf"


def test_server_error_on_system1_is_temporary(monkeypatch):
    """Đúng ca thật của C23TGV-125/126: bill2 không có, bill1 trả 'Lỗi phát sinh'."""
    _setup(monkeypatch, [], {API2: [NOT_HERE], API1: [SERVER_ERR]})
    with pytest.raises(PdfFetchError) as ei:
        xcyber.XcyberAdapter().fetch_pdf(INV)
    assert ei.value.kind == "server"
    assert "Lỗi phát sinh" in str(ei.value)


@pytest.mark.parametrize("download, file, kind", [
    ((EXPIRED,), None, "no_pdf"),
    ((TOKEN,), _Resp(None, 302, b""), "bad_content"),                       # token đã dùng → redirect
    ((TOKEN,), _Resp(None, content=b"<html>" + b"x" * 2000), "bad_content"),
    ((TOKEN,), "other_invoice", "bad_content"),                              # PDF của HĐ khác
    ((_Resp(None, 403),), None, "bad_content"),
    ((_Resp({"result": None, "error": {"message": "boom"}}),), None, "other"),
])
def test_download_errors(monkeypatch, download, file, kind):
    if file == "other_invoice":
        file = _Resp(None, content=_pdf_with("So 99999"))
    _setup(monkeypatch, [], {API2: [FOUND]}, download=download, file=file)
    with pytest.raises(PdfFetchError) as ei:
        xcyber.XcyberAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


@pytest.mark.parametrize("inv", [
    {"shdon": 1, "ma_tra_cuu": "", "tt_khac": {}},
    {"shdon": 1, "ma_tra_cuu": "ABC123", "tt_khac": {}},                     # sai độ dài
])
def test_no_code(inv):
    with pytest.raises(PdfFetchError) as ei:
        xcyber.XcyberAdapter().fetch_pdf(inv)
    assert ei.value.kind == "no_code"


def test_lookup_code_prefers_tt_khac():
    assert xcyber.XcyberAdapter.lookup_code({"tt_khac": {"MaTraCuu": " A1 "}, "ma_tra_cuu": "B"}) == "A1"
    assert xcyber.XcyberAdapter.lookup_code({"tt_khac": {}, "ma_tra_cuu": "Y2"}) == "Y2"


def test_invoice_from_link():
    a = xcyber.XcyberAdapter()
    assert a.invoice_from_link("https://tracuuhoadon2.xcyber.vn/", "2MG82ARHG0M6", "")["ma_tra_cuu"] == "2MG82ARHG0M6"
    assert a.invoice_from_link("https://tracuuhoadon2.xcyber.vn/", None, "") is None
    assert a.invoice_from_link("https://tracuuhoadon2.xcyber.vn/", "short", "") is None


def test_declares_mst_tcgp():
    a = xcyber.XcyberAdapter()
    assert a.source_id == "xcyber" and a.mst_tcgp == ("0105232093",)
