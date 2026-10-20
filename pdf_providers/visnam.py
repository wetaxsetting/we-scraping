# pdf_providers/visnam.py
"""
Visnam (vin-hoadon) — MSTTCGP 0401486901.
Tra cứu: https://tracuu.vin-hoadon.com (Angular + API ABP /api/services/hddt/...)

Mã tra cứu: TTKhac "QuanLy_SoBaoMat" (12 ký tự, vd 29W2XF3XMCY0) = ma_tra_cuu của parser.
TTKhac "IdHoaDon" là id nội bộ, không dùng để tra.

Quy trình (khảo sát 10/2026 trên HĐ thật C26TPH-50, C26TPH-44):
  1. POST {api}/RefreshCaptcha                 → result{key, image="data:image/png;base64,..."}
       PNG 1500x350, 4 chữ số → model g_1500x350. Captcha gắn với `key` (không cookie).
  2. POST {api}/TraCuu  JSON {key, captcha, maSoBiMat}
       → result.status "1": filePdf{fileName, fileData=PDF base64} (server dựng sẵn), hoaDon{...}
       → result.status 2 + message "Mã xác thực không hợp lệ"   : sai captcha
       → result.status 2 + message khác                          : không thấy HĐ
  {api} = /api/services/hddt/TraCuuHoaDon. Mọi người bán dùng chung cổng này.
"""
from __future__ import annotations

import base64
import binascii
import json
import unicodedata

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request, solve_captcha

BASE = "https://tracuu.vin-hoadon.com"
API = f"{BASE}/api/services/hddt/TraCuuHoaDon"
CAPTCHA_GROUP = "g_1500x350"
CAPTCHA_TRIES = 3
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json",
            "Referer": f"{BASE}/", "Origin": BASE}
_MSG_CAPTCHA = ("mã xác thực", "captcha", "mã kiểm tra")


def _norm(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip().lower()


class _WrongCaptcha(Exception):
    pass


class VisnamAdapter(PdfAdapter):
    source_id = "visnam"
    name = "Visnam (vin-hoadon)"
    mst_tcgp = ("0401486901",)
    hosts = ("vin-hoadon.com",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail Visnam: ...?mstdoanhnghiep=<MST bán>&masobimat=<mã>. Link hủy nhận mail không có mã → None."""
        p = link_params(url)
        secret = p.get("masobimat")
        if not secret:
            return None
        return {"nbmst": p.get("mstdoanhnghiep"), "ma_tra_cuu": secret,
                "tt_khac": {"QuanLy_SoBaoMat": secret}}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("QuanLy_SoBaoMat") or invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'QuanLy_SoBaoMat' (mã tra cứu Visnam)", "no_code")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            s = new_session()
            try:
                result = self._search(s, code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"Visnam từ chối captcha {CAPTCHA_TRIES} lần", "captcha")
        pdf = self._pdf_from(result, code)
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    @staticmethod
    def _result(r, what: str) -> dict:
        try:
            js = r.json()
        except ValueError as e:
            raise PdfFetchError(f"Visnam {what} trả không phải JSON (HTTP {r.status_code})",
                                "bad_content") from e
        if not isinstance(js, dict) or not isinstance(js.get("result"), dict):
            err = js.get("error") if isinstance(js, dict) else None
            raise PdfFetchError(f"Visnam {what} lỗi (HTTP {r.status_code}): {err or js!r}"[:300], "other")
        return js["result"]

    def _search(self, s, code: str) -> dict:
        cap = self._result(request(s, "POST", f"{API}/RefreshCaptcha", headers=_HEADERS), "RefreshCaptcha")
        img, key = str(cap.get("image") or ""), cap.get("key")
        try:
            img_bytes = base64.b64decode(img.split(",", 1)[-1])
        except (binascii.Error, ValueError):
            img_bytes = b""
        if not img_bytes or not key:
            raise PdfFetchError("Visnam không trả ảnh captcha", "server")
        answer = solve_captcha(img_bytes, CAPTCHA_GROUP, min_conf=0.8)
        if not (len(answer) == 4 and answer.isdigit()):
            raise _WrongCaptcha(f"model đọc {answer!r}, captcha Visnam là 4 chữ số")   # không gửi bừa
        r = request(s, "POST", f"{API}/TraCuu", headers=_HEADERS,
                    data=json.dumps({"key": key, "captcha": answer, "maSoBiMat": code}))
        res = self._result(r, "TraCuu")
        if str(res.get("status")) == "1":
            return res
        msg = _norm(res.get("message"))
        if any(k in msg for k in _MSG_CAPTCHA):
            raise _WrongCaptcha(msg)
        raise PdfFetchError(f"Visnam không tìm thấy HĐ (mã {code}): {res.get('message') or 'không có thông báo'}",
                            "no_pdf")

    @staticmethod
    def _pdf_from(res: dict, code: str) -> bytes:
        data = (res.get("filePdf") or {}).get("fileData")
        if not data:
            raise PdfFetchError(f"Visnam thấy HĐ (mã {code}) nhưng không kèm PDF", "no_pdf")
        try:
            pdf = base64.b64decode(data)
        except (binascii.Error, ValueError) as e:
            raise PdfFetchError("Visnam trả PDF base64 hỏng", "bad_content") from e
        if not is_pdf(pdf):
            raise PdfFetchError("Visnam trả nội dung không phải PDF", "bad_content")
        return pdf


register(VisnamAdapter())
