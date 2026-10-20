"""Adapter Visnam / vin-hoadon (HTTP giả lập, không cần mạng).

Luồng thật đã kiểm trên HĐ thật 10/2026 (C26TPH-50, C26TPH-44); captcha g_1500x350 đọc đúng 11/11.

Run::

    pytest tests/test_pdf_visnam.py -q
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

import pdf_providers                       # noqa: E402
from pdf_providers import PdfFetchError    # noqa: E402
from pdf_providers import common           # noqa: E402
from pdf_providers import visnam           # noqa: E402


def _pdf_with(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    for i in range(40):                      # đủ dài để vượt ngưỡng is_pdf
        page.insert_text((40, 40 + i * 18), f"{text} dong {i}")
    data = doc.tobytes()
    doc.close()
    assert common.is_pdf(data)
    return data


class _Resp:
    def __init__(self, js=None, status=200):
        self.status_code, self._js = status, js

    def json(self):
        if self._js is None:
            raise ValueError("not json")
        return self._js


def _abp(result):
    return _Resp({"result": result, "success": True, "error": None})


CAPTCHA = _abp({"status": "0", "key": "k-1",
                "image": "data:image/png;base64," + base64.b64encode(b"png").decode()})
WRONG_CAPTCHA = _abp({"status": 2, "message": "Mã xác thực không hợp lệ", "fileStatus": 0})
NOT_FOUND = _abp({"status": 2, "message": "Mã số không đúng!", "fileStatus": 0})


def _found(pdf: bytes):
    return _abp({"status": "1", "filePdf": {"fileName": "x.pdf", "fileType": "application/pdf",
                                             "fileData": base64.b64encode(pdf).decode()}})


def _setup(monkeypatch, tracuu, calls, answers=("1234",)):
    """tracuu = list phản hồi lần lượt cho các lệnh TraCuu."""
    tracuu = list(tracuu)
    answers = list(answers)

    def fake(session, method, url, **kw):
        calls.append((method, url, kw))
        if url.endswith("/RefreshCaptcha"):
            return CAPTCHA
        if url.endswith("/TraCuu"):
            return tracuu.pop(0)
        raise AssertionError(f"không có route cho {method} {url}")
    monkeypatch.setattr(visnam, "request", fake)
    monkeypatch.setattr(visnam, "new_session", lambda: object())
    monkeypatch.setattr(visnam, "solve_captcha",
                        lambda img, group, min_conf=0.5: answers.pop(0) if len(answers) > 1 else answers[0])


INV = {"shdon": 50, "khhdon": "C26TPH", "khmshdon": "1", "nbmst": "0304892753",
       "ma_tra_cuu": "29W2XF3XMCY0",
       "tt_khac": {"QuanLy_SoBaoMat": "29W2XF3XMCY0", "IdHoaDon": "2838706"}}


def test_ok_posts_key_captcha_and_secret(monkeypatch):
    calls = []
    _setup(monkeypatch, [_found(_pdf_with("So 50"))], calls)
    assert visnam.VisnamAdapter().fetch_pdf(INV).startswith(b"%PDF")
    tra = [kw for _, u, kw in calls if u.endswith("/TraCuu")]
    assert json.loads(tra[0]["data"]) == {"key": "k-1", "captcha": "1234", "maSoBiMat": "29W2XF3XMCY0"}


def test_accepts_db_shaped_invoice(monkeypatch):
    inv = dict(INV, khmshdon=1, tong_tien=Decimal("28080000"), ngay_lap=date(2026, 10, 6))
    _setup(monkeypatch, [_found(_pdf_with("So 50"))], [])
    assert visnam.VisnamAdapter().fetch_pdf(inv).startswith(b"%PDF")


def test_retries_wrong_captcha_with_new_captcha(monkeypatch):
    calls = []
    _setup(monkeypatch, [WRONG_CAPTCHA, _found(_pdf_with("So 50"))], calls)
    assert visnam.VisnamAdapter().fetch_pdf(INV).startswith(b"%PDF")
    assert sum(u.endswith("/RefreshCaptcha") for _, u, _ in calls) == 2


def test_gives_up_after_tries(monkeypatch):
    _setup(monkeypatch, [WRONG_CAPTCHA] * visnam.CAPTCHA_TRIES, [])
    with pytest.raises(PdfFetchError) as ei:
        visnam.VisnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"


def test_does_not_send_non_digit_answer(monkeypatch):
    calls = []
    _setup(monkeypatch, [], calls, answers=("12a4",))
    with pytest.raises(PdfFetchError) as ei:
        visnam.VisnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == "captcha"
    assert not any(u.endswith("/TraCuu") for _, u, _ in calls)


@pytest.mark.parametrize("resp, kind", [
    (NOT_FOUND, "no_pdf"),
    (_abp({"status": "1", "filePdf": None}), "no_pdf"),
    (_abp({"status": "1", "filePdf": {"fileData": base64.b64encode(b"<html>" + b"x" * 2000).decode()}}),
     "bad_content"),
    ("other_invoice", "bad_content"),                                      # PDF của HĐ khác
    (_Resp(None, 403), "bad_content"),
    (_Resp({"result": None, "error": {"message": "boom"}}), "other"),
])
def test_errors(monkeypatch, resp, kind):
    if resp == "other_invoice":
        resp = _found(_pdf_with("So 99999"))
    _setup(monkeypatch, [resp], [])
    with pytest.raises(PdfFetchError) as ei:
        visnam.VisnamAdapter().fetch_pdf(INV)
    assert ei.value.kind == kind


def test_no_code():
    with pytest.raises(PdfFetchError) as ei:
        visnam.VisnamAdapter().fetch_pdf({"shdon": 1, "ma_tra_cuu": "", "tt_khac": {"IdHoaDon": "1"}})
    assert ei.value.kind == "no_code"


def test_lookup_code_prefers_tt_khac():
    assert visnam.VisnamAdapter.lookup_code({"tt_khac": {"QuanLy_SoBaoMat": " A1 "}, "ma_tra_cuu": "B"}) == "A1"
    assert visnam.VisnamAdapter.lookup_code({"tt_khac": {}, "ma_tra_cuu": "Y2"}) == "Y2"


def test_registered_for_mst_tcgp():
    assert pdf_providers.get_adapter("visnam").mst_tcgp == ("0401486901",)
    assert pdf_providers.adapter_for_mst_tcgp("0401486901").source_id == "visnam"
