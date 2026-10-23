"""
VNISC — MSTTCGP 0102331762. Tra cứu: https://invoice.abcsys.vn/Invoice/Search (ASP.NET MVC).
(KHÔNG phải nền tảng vninvoice.vn / nhóm g_180x50_vn — xem CLAUDE.md 1.5 mục 3/4.)

Mã tra cứu: TTKhac "MTCuu" = "<Agentcode>-<12 ký tự>" (vd BGT-F9WGNAKDCVYI, DAV-WU1PEOGBQUO9);
Agentcode cũng ghi riêng ở TTKhac "Agentcode". Mail ghi link /Invoice/Search?ID=<mã> (có khi
có dấu cách thừa trước mã: ID=%20BGT-...).

Quy trình (khảo sát + kiểm trên HĐ thật 10/2026):
  1. GET  /Invoice/Search                 → cookie phiên
  2. GET  /Capchar/ShowCaptchaImage       → JPEG 100x46, đúng 6 CHỮ SỐ đen trên nền cam nhạt,
       không nhiễu. Server KIỂM captcha thật (sai/trống → "Mã xác nhận không chính xác").
       ddddocr đọc thẳng: server nhận 20/20.
  3. POST /Invoice/Search  form {txtSoHoaDon=<mã>, txtCapChar}
       → thấy: chuyển sang /Invoice/ViewInfomation, có link
         /Invoice/ViewPDF?InvoiceNumber=<Agent><năm>EIS_<số 8 chữ số>  (+ DownloadXML)
       → không thấy: ở lại /Invoice/Search, "Không tìm thấy số hóa đơn trùng khớp..."
  4. GET  /Invoice/ViewPDF?InvoiceNumber=...  → application/pdf.
Không có reCAPTCHA.
"""
from __future__ import annotations

import html
import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .ocr_ddddocr import ocr_text

BASE = "https://invoice.abcsys.vn"
SEARCH = BASE + "/Invoice/Search"
CAPTCHA_TRIES = 4
CAPTCHA_LEN = 6
_CODE_RE = re.compile(r"^[A-Za-z0-9]{2,10}-[A-Za-z0-9]{6,30}$")
_ERR_RE = re.compile(r"\$\('#content-error'\)\.html\('([^']*)'\)")
_PDF_RE = re.compile(r'href="(/Invoice/ViewPDF\?InvoiceNumber=[^"]+)"')


class _WrongCaptcha(Exception):
    pass


def solve(img: bytes) -> str:
    text = re.sub(r"\D", "", ocr_text(img))
    if len(text) != CAPTCHA_LEN:
        raise _WrongCaptcha(f"ddddocr đọc {text!r}, captcha VNISC là {CAPTCHA_LEN} chữ số")
    return text


class VniscAdapter(PdfAdapter):
    source_id = "vnisc"
    name = "VNISC (invoice.abcsys.vn)"
    mst_tcgp = ("0102331762",)
    hosts = ("invoice.abcsys.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link /Invoice/Search?ID=<mã> hoặc mã MTCuu trong mail. Không mở link."""
        secret = (link_params(url).get("id") or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"MTCuu": secret}}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("MTCuu") or tt_khac.get("MTCUU") or invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có TTKhac 'MTCuu' (mã tra cứu VNISC)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu VNISC không đúng dạng <đại lý>-<mã>: {code!r}", "no_code")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            s = new_session()
            try:
                link, referer = self._search(s, code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"VNISC từ chối/không đọc được captcha {CAPTCHA_TRIES} lần", "captcha")
        r = request(s, "GET", BASE + link, headers={"Referer": referer})
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(
                f"VNISC trả nội dung không phải PDF ({r.headers.get('Content-Type')}, HTTP {r.status_code})",
                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def _search(self, s, code: str):
        """→ (link ViewPDF, referer). Captcha gắn cookie phiên → phiên mới mỗi lượt."""
        request(s, "GET", SEARCH)
        img = request(s, "GET", BASE + "/Capchar/ShowCaptchaImage", headers={"Referer": SEARCH}).content
        answer = solve(img)
        r = request(s, "POST", SEARCH, headers={"Referer": SEARCH},
                    data={"txtSoHoaDon": code, "txtCapChar": answer})
        errors = " | ".join(html.unescape(x) for x in _ERR_RE.findall(r.text))
        if "xác nhận" in errors.lower():
            raise _WrongCaptcha(errors)
        if "không tìm thấy" in errors.lower():
            raise PdfFetchError(f"VNISC không tìm thấy HĐ với mã {code}", "no_pdf")
        m = _PDF_RE.search(r.text)
        if not m:
            raise PdfFetchError(f"Trang kết quả VNISC không có link PDF ({errors or 'không có thông báo'})",
                                "bad_content")
        return html.unescape(m.group(1)), getattr(r, "url", SEARCH)


register(VniscAdapter())
