"""
SmartSign — Công ty CP chữ ký số VI NA (Hóa đơn điện tử Vi Na), MSTTCGP 0309612872.
Tra cứu: https://tracuuhd.smartsign.com.vn/  (ASP.NET WebForms, postback)

Mã tra cứu: TTKhac "Matracuu" (12 ký tự, vd 26K8049787RX), TTKhac "Trangtracuu" ghi
tracuuhd.smartsign.com.vn. HĐ 2023 không có TTKhac → không tra được ('no_code').
Mail của SmartSign ghi link ehd./hddt./tracuuhd.smartsign.com.vn kèm mã — cùng 1 hệ thống,
luôn tra ở tracuuhd.

Quy trình (khảo sát + kiểm trên HĐ thật 10/2026):
  1. GET  /                      → __VIEWSTATE, __EVENTVALIDATION... (cookie phiên)
  2. GET  /Pages/CImage.aspx     → JPEG 150x39, 2–5 ký tự (thường + HOA + số), sọc chéo đỏ.
       Server KIỂM captcha thật (sai → tabErrContent "Mã captcha không đúng") và phân biệt
       hoa/thường (hạ hết về chữ thường: 25/40 qua, giữ nguyên: 37/40).
       Đọc bằng ddddocr sau khi bỏ sọc đỏ (giữ điểm có max(R,G,B) < 110): server nhận 54/60
       (ảnh thô chỉ 11/20). Model g_150x39 của ta chỉ 2,3% nên không dùng.
  3. POST /  (multipart, như form) txtCode + txtCapcha + Action=SHOW_INVOICE
       → thấy: lblXML = mã tra cứu, divNotFoundActions display:none
       → không thấy: tabErrContent rỗng, divNotFoundActions display:flex ("Không tìm thấy hóa đơn")
  4. POST /  với __EVENTTARGET=ctl00$ContentPlaceHolder1$LinkButtonDownloadPDF (viewstate của
       trang kết quả) → application/pdf (bản thể hiện có số HĐ).
Trang có nạp recaptcha/api.js nhưng không có widget, server không đòi token.
"""
from __future__ import annotations

import html
import io
import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .ocr_ddddocr import ocr_text

BASE = "https://tracuuhd.smartsign.com.vn/"
CAPTCHA_TRIES = 5
CAPTCHA_LEN = (2, 6)                     # thấy 2–5 ký tự; 1 ký tự = ddddocr đọc sót
_F = "ctl00$ContentPlaceHolder1$"
_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,30}$")
_HIDDEN_RE = re.compile(r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"')
_ERR_RE = re.compile(r'id="ContentPlaceHolder1_tabErrContent">([^<]*)<')
_FOUND_RE = re.compile(r'id="ContentPlaceHolder1_lblXML"[^>]*>([^<]*)<')
_NOT_FOUND_RE = re.compile(r'id="divNotFoundActions"[^>]*display:\s*flex')


class _WrongCaptcha(Exception):
    pass


def clean_captcha(img: bytes) -> bytes:
    """Bỏ sọc chéo đỏ + chấm nhiễu sáng: chỉ giữ điểm tối (chữ) → PNG đen trên trắng."""
    from PIL import Image
    im = Image.open(io.BytesIO(img)).convert("RGB")
    out = Image.new("L", im.size, 255)
    src, dst = im.load(), out.load()
    for x in range(im.width):
        for y in range(im.height):
            if max(src[x, y]) < 110:     # đỏ (255,0,0) có max 255 → bỏ; chữ đen/xám đậm → giữ
                dst[x, y] = 0
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def solve(img: bytes) -> str:
    """Đáp án captcha (giữ nguyên hoa/thường), hoặc _WrongCaptcha nếu đọc ra độ dài vô lý."""
    try:
        png = clean_captcha(img)
    except Exception as e:  # noqa: BLE001
        raise _WrongCaptcha(f"ảnh captcha không đọc được: {e}") from e
    text = re.sub(r"[^A-Za-z0-9]", "", ocr_text(png))
    if not CAPTCHA_LEN[0] <= len(text) <= CAPTCHA_LEN[1]:
        raise _WrongCaptcha(f"ddddocr đọc {text!r}, captcha SmartSign dài 2–5 ký tự")
    return text


def _hidden(page: str) -> dict:
    return {k: html.unescape(v) for k, v in _HIDDEN_RE.findall(page)}


def _multipart(fields: dict) -> dict:
    return {k: (None, v) for k, v in fields.items()}


class SmartSignAdapter(PdfAdapter):
    source_id = "smartsign"
    name = "SmartSign (Vi Na)"
    mst_tcgp = ("0309612872",)
    hosts = ("smartsign.com.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail SmartSign: link trang chủ cổng + 'Mã nhận hóa đơn'. Không mở link."""
        p = link_params(url)
        secret = (code or p.get("code") or p.get("matracuu") or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Matracuu": secret}}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for k, v in tt_khac.items():
            if k.lower().replace(" ", "") in ("matracuu", "manhanhoadon") and str(v or "").strip():
                return str(v).strip()
        return str(invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có TTKhac 'Matracuu' (mã nhận HĐ SmartSign)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu SmartSign không hợp lệ: {code!r}", "no_code")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            s = new_session()
            try:
                page = self._search(s, code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"SmartSign từ chối/không đọc được captcha {CAPTCHA_TRIES} lần",
                                        "captcha")
        pdf = self._download(s, page, code)
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def _search(self, s, code: str) -> str:
        """→ HTML trang kết quả (có viewstate để postback tải PDF)."""
        home = request(s, "GET", BASE)
        fields = _hidden(home.text)
        if "__VIEWSTATE" not in fields:
            raise PdfFetchError("Không thấy form tra cứu trên tracuuhd.smartsign.com.vn", "server")
        img = request(s, "GET", BASE + "Pages/CImage.aspx", headers={"Referer": BASE}).content
        answer = solve(img)
        fields.update({_F + "txtCode": code, _F + "txtCapcha": answer, "Action": "SHOW_INVOICE"})
        r = request(s, "POST", BASE, files=_multipart(fields), headers={"Referer": BASE})
        err = html.unescape((_ERR_RE.search(r.text) or [None, ""])[1]).strip()
        if "captcha" in err.lower():
            raise _WrongCaptcha(err)
        if err:
            raise PdfFetchError(f"SmartSign báo lỗi khi tra mã {code}: {err}"[:300], "other")
        found = html.unescape((_FOUND_RE.search(r.text) or [None, ""])[1]).strip()
        if found.upper() == code.upper():
            return r.text
        if _NOT_FOUND_RE.search(r.text):
            raise PdfFetchError(f"SmartSign không tìm thấy HĐ với mã {code}", "no_pdf")
        raise PdfFetchError(f"Trang kết quả SmartSign lạ (mã {code}, HTTP {r.status_code})", "bad_content")

    def _download(self, s, page: str, code: str) -> bytes:
        fields = _hidden(page)
        fields.update({"__EVENTTARGET": _F + "LinkButtonDownloadPDF", "__EVENTARGUMENT": "",
                       _F + "txtCode": code})
        r = request(s, "POST", BASE, files=_multipart(fields), headers={"Referer": BASE})
        if not is_pdf(r.content):
            raise PdfFetchError(
                f"SmartSign trả nội dung không phải PDF ({r.headers.get('Content-Type')}, "
                f"HTTP {r.status_code})", "bad_content")
        return r.content


register(SmartSignAdapter())
