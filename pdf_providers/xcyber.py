# pdf_providers/xcyber.py
"""
CyberLotus (xcyber / CyberBill) — MSTTCGP 0105232093.
Tra cứu: https://tracuuhoadon2.xcyber.vn (Angular + API ABP /api/services/hddt/TraCuuHoaDon/...)

Mã tra cứu: TTKhac "MaTraCuu" (12 ký tự, vd 2MG82ARHG0M6) = ma_tra_cuu của parser.
Trang chỉ nhận mã dài 12 hoặc 15 ký tự (kiểm ở client).

CyberLotus có HAI hệ thống, cùng mã nguồn trang, khác API — HĐ nằm ở hệ thống nào thì chỉ
hệ thống đó tra ra (khảo sát 10/2026 trên HĐ thật C24TGL-36, C23TGV-125/126):
  * tracuuhoadon2.xcyber.vn → API https://bill2app.xcyber.vn   (tracuuhoadon.cyberbill.vn chuyển về đây)
  * tracuuhoadon1.xcyber.vn → API https://bill1app.xcyber.vn   (tracuu.cyberbill.vn chuyển về đây)
Hệ thống này trả status 4 ("không có kết quả — tra trên link dự phòng") thì thử hệ thống kia.

Quy trình (mỗi hệ thống):
  1. POST {api}/RefreshCaptcha        → result{key, image="data:image/jpeg;base64,<PNG>"}
       PNG 300x75 RGBA, 5 chữ số → model g_300x75. Captcha gắn với `key` (không cookie).
       Server KIỂM captcha thật (sai → status 2 "Mã xác thực không hợp lệ").
  2. POST {api}/TraCuu   JSON {key, captcha, maSoBiMat}
       → status 1: tìm thấy; result.key = khóa phiên MỚI để tải file (html + XML base64 kèm theo)
       → status 2: sai captcha · status 3: lỗi phía server ("Lỗi phát sinh") · status 4: không thấy
  3. POST {api}/DownloadPdf  JSON {key=<khóa ở bước 2>, maSoBiMat}  → result{fileToken, fileName, fileType}
       (khóa sai / hết hạn → status 2 "Phiên làm việc đã hết")
  4. GET  {api_host}/File/DownloadTempFile?fileType&fileToken&fileName → PDF (Content-Type ghi text/xml).
       fileToken dùng 1 lần: gọi lại thì server chuyển hướng vòng → tắt redirect.
Trang có nạp recaptcha/api.js nhưng không có widget, server không đòi token reCAPTCHA.
Cùng nền tảng xcyber còn Liên Sơn / My-Invoice — chưa kiểm trên HĐ thật nên chưa nhận MST của họ.
"""
from __future__ import annotations

import base64
import binascii
import json
import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request, solve_captcha

# (API, trang tra cứu) theo thứ tự thử: hệ thống 2 (mới) trước, hệ thống 1 sau.
SYSTEMS = (
    ("https://bill2app.xcyber.vn", "https://tracuuhoadon2.xcyber.vn"),
    ("https://bill1app.xcyber.vn", "https://tracuuhoadon1.xcyber.vn"),
)
API_PATH = "/api/services/hddt/TraCuuHoaDon"
CAPTCHA_GROUP = "g_300x75"
CAPTCHA_LEN = 5
CAPTCHA_TRIES = 3
_CODE_RE = re.compile(r"^[A-Za-z0-9]{12}$|^[A-Za-z0-9]{15}$")


class _WrongCaptcha(Exception):
    pass


class _NotHere(Exception):
    """Hệ thống này không có HĐ (status 4) → thử hệ thống kia."""


class XcyberAdapter(PdfAdapter):
    source_id = "xcyber"
    name = "CyberLotus (xcyber)"
    mst_tcgp = ("0105232093",)
    hosts = ("xcyber.vn", "cyberbill.vn")

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail CyberLotus: mã tra cứu 12/15 ký tự (trong mail hoặc tham số link). Không mở link."""
        p = link_params(url)
        secret = (code or p.get("masobimat") or p.get("matracuu") or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"MaTraCuu": secret}}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("MaTraCuu") or invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'MaTraCuu' (mã tra cứu CyberLotus)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu CyberLotus phải dài 12/15 ký tự: {code!r}", "no_code")
        server_errors = []
        for api_host, site in SYSTEMS:
            try:
                pdf = self._fetch_on(api_host, site, code)
            except _NotHere:
                continue
            except PdfFetchError as e:
                if e.kind != "server":
                    raise
                server_errors.append(str(e))          # hệ thống này lỗi → vẫn thử hệ thống kia
                continue
            check_pdf_is_invoice(pdf, invoice)
            return pdf
        if server_errors:
            raise PdfFetchError("; ".join(server_errors)[:500], "server")
        raise PdfFetchError(f"CyberLotus không tìm thấy HĐ (mã {code}) trên cả 2 hệ thống", "no_pdf")

    def _fetch_on(self, api_host: str, site: str, code: str) -> bytes:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Origin": site, "Referer": f"{site}/"}
        api = api_host + API_PATH
        for attempt in range(1, CAPTCHA_TRIES + 1):
            s = new_session()
            try:
                key = self._search(s, api, headers, code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"CyberLotus ({api_host}) từ chối captcha {CAPTCHA_TRIES} lần",
                                        "captcha")
        return self._download(s, api_host, api, headers, key, code)

    @staticmethod
    def _result(r, what: str) -> dict:
        try:
            js = r.json()
        except ValueError as e:
            raise PdfFetchError(f"CyberLotus {what} trả không phải JSON (HTTP {r.status_code})",
                                "bad_content") from e
        if not isinstance(js, dict) or not isinstance(js.get("result"), dict):
            err = js.get("error") if isinstance(js, dict) else None
            raise PdfFetchError(f"CyberLotus {what} lỗi (HTTP {r.status_code}): {err or js!r}"[:300], "other")
        return js["result"]

    def _search(self, s, api: str, headers: dict, code: str) -> str:
        """Tra cứu → khóa phiên để tải file."""
        cap = self._result(request(s, "POST", f"{api}/RefreshCaptcha", headers=headers), "RefreshCaptcha")
        img, key = str(cap.get("image") or ""), cap.get("key")
        try:
            img_bytes = base64.b64decode(img.split(",", 1)[-1])
        except (binascii.Error, ValueError):
            img_bytes = b""
        if not img_bytes or not key:
            raise PdfFetchError("CyberLotus không trả ảnh captcha", "server")
        answer = solve_captcha(img_bytes, CAPTCHA_GROUP, min_conf=0.8)
        if not (len(answer) == CAPTCHA_LEN and answer.isdigit()):
            raise _WrongCaptcha(f"model đọc {answer!r}, captcha CyberLotus là {CAPTCHA_LEN} chữ số")
        r = request(s, "POST", f"{api}/TraCuu", headers=headers,
                    data=json.dumps({"key": key, "captcha": answer, "maSoBiMat": code}))
        res = self._result(r, "TraCuu")
        status, msg = str(res.get("status")), (res.get("message") or "").strip()
        if status == "1":
            if not res.get("key"):
                raise PdfFetchError(f"CyberLotus thấy HĐ (mã {code}) nhưng không trả khóa tải file", "other")
            return res["key"]
        if status == "2":
            raise _WrongCaptcha(msg)
        if status == "4":
            raise _NotHere(code)
        if status == "3":
            # Đã gặp thật: HĐ C23TGV (2023) trên bill1app luôn trả "Lỗi phát sinh" — lỗi phía server.
            raise PdfFetchError(f"CyberLotus ({api}) lỗi khi tra mã {code}: {msg or 'status 3'}", "server")
        raise PdfFetchError(f"CyberLotus TraCuu trả status lạ {status!r}: {msg}"[:300], "other")

    def _download(self, s, api_host: str, api: str, headers: dict, key: str, code: str) -> bytes:
        r = request(s, "POST", f"{api}/DownloadPdf", headers=headers,
                    data=json.dumps({"key": key, "maSoBiMat": code}))
        res = self._result(r, "DownloadPdf")
        token = res.get("fileToken")
        if not token:
            raise PdfFetchError(f"CyberLotus không cho tải PDF (mã {code}): {res.get('message') or res!r}"[:300],
                                "no_pdf")
        r = request(s, "GET", f"{api_host}/File/DownloadTempFile", allow_redirects=False,
                    params={"fileType": res.get("fileType") or "", "fileToken": token,
                            "fileName": res.get("fileName") or ""})
        pdf = r.content if r.status_code == 200 else b""
        if not is_pdf(pdf):
            raise PdfFetchError(f"CyberLotus trả nội dung không phải PDF (HTTP {r.status_code})", "bad_content")
        return pdf


register(XcyberAdapter())
