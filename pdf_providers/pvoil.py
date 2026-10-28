"""
PVOIL (Tổng công ty Dầu Việt Nam) — MSTTCGP 0305795054.
Tra cứu: https://hoadon.pvoil.vn  (ASP.NET MVC, cùng họ phần mềm Megabiz/HILO).

Mã tra cứu = "Mã tra cứu" trên cổng = TTKhac/ma_tra_cuu của HĐ.

Quy trình (khảo sát 27/10/2026):
  1. GET  /Invoice/Search      → form (checkbox TT78, input key, captcha)
  2. GET  /Captcha/Show        → PNG 194x35, 4 chữ số, gắn cookie phiên → model g_194x35
  3. POST /Invoice/search      TT78(true/false), key, captch
       → captcha sai: div.alert "Nhập sai mã capcha !"
       → không thấy:  div.alert "Không tìm thấy dữ liệu !"
       → thấy: HTML hóa đơn có nút gọi downloadInvPDF(<id>, '<pattern>')
  4. GET  /Invoice/DownloadPDF?id=<id>&pattern=<pattern>   → application/pdf (có số HĐ)

Trang không có reCAPTCHA. Đã kiểm 27/10/2026: captcha g_194x35 server nhận 10/10
(gửi mã bịa → "Không tìm thấy dữ liệu" = captcha qua; captcha sai → "Nhập sai mã capcha").
CHƯA có HĐ PVOIL thật trong hệ thống nên bước 4 (tải PDF) chưa chạy thật — PDF luôn qua
check_pdf_is_invoice nên sai cũng không lưu nhầm.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request, solve_captcha

BASE = "https://hoadon.pvoil.vn"
CAPTCHA_GROUP = "g_194x35"
CAPTCHA_TRIES = 4

# downloadInvPDF(123, 'C25TAA')  — KHÔNG khớp định nghĩa hàm downloadInvPDF(idInvoice, pattern)
_DL_RE = re.compile(r"downloadInvPDF\(\s*(\d+)\s*,\s*['\"]([^'\"]+)['\"]\s*\)")
_MSG_CAPTCHA = "sai mã capcha"
_MSG_NOT_FOUND = "không tìm thấy dữ liệu"


class _WrongCaptcha(Exception):
    pass


class PvoilAdapter(PdfAdapter):
    source_id = "pvoil"
    name = "PVOIL"
    mst_tcgp = ("0305795054",)
    hosts = ("hoadon.pvoil.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        host = (urlsplit(url).hostname or "").lower()
        if host != "hoadon.pvoil.vn":
            return None
        p = link_params(url)
        secret = (code or p.get("key") or p.get("code") or "").strip()
        if not secret:
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"MaTraCuu": secret}, "lookup_url": BASE}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("MaTraCuu") or tt_khac.get("Matracuu") or tt_khac.get("Fkey")
                   or invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có mã tra cứu PVOIL", "no_code")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            try:
                inv_id, pattern = self._search(code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"PVOIL từ chối captcha {CAPTCHA_TRIES} lần", "captcha")
        r = request(new_session(), "GET", f"{BASE}/Invoice/DownloadPDF",
                    params={"id": inv_id, "pattern": pattern},
                    headers={"Referer": f"{BASE}/Invoice/Search"})
        if not is_pdf(r.content):
            raise PdfFetchError(
                f"PVOIL DownloadPDF trả không phải PDF ({r.headers.get('Content-Type')})",
                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def _search(self, code: str):
        """→ (id, pattern) để tải PDF. Captcha gắn cookie phiên → phiên mới mỗi lượt."""
        s = new_session()
        request(s, "GET", f"{BASE}/Invoice/Search")
        img = request(s, "GET", f"{BASE}/Captcha/Show", headers={"Referer": f"{BASE}/Invoice/Search"})
        if img.status_code != 200 or not img.content:
            raise PdfFetchError("Không lấy được captcha PVOIL", "server")
        answer = solve_captcha(img.content, CAPTCHA_GROUP)
        r = request(s, "POST", f"{BASE}/Invoice/search",
                    data={"TT78": ["true", "false"], "key": code, "captch": answer},
                    headers={"Referer": f"{BASE}/Invoice/Search", "Origin": BASE})
        page = html.unescape(r.text)
        low = page.lower()
        if _MSG_CAPTCHA in low:
            raise _WrongCaptcha("Nhập sai mã capcha")
        if _MSG_NOT_FOUND in low:
            raise PdfFetchError(f"PVOIL không tìm thấy HĐ với mã {code}", "no_pdf")
        m = _DL_RE.search(page)
        if not m:
            raise PdfFetchError("Trang kết quả PVOIL không có link tải PDF", "bad_content")
        return m.group(1), m.group(2)


register(PvoilAdapter())
