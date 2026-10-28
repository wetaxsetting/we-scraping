"""
Megabiz (vinvoice) — MSTTCGP 0105844836.
Tra cứu: https://tracuu.vinvoice.vn  (ASP.NET MVC, phần mềm Megabiz/vinvoice).

KHÔNG phải phần mềm EasyInvoice: dù cùng có /Captcha/Show, endpoint và form khác hẳn
(EasyInvoice: /Search/Search + /Invoice/DownloadPdf...; Megabiz: /Invoice/SearchTaxInv +
/Invoice/DownloadTaxInv). Không dùng lại adapter easyinvoice được.

Mã tra cứu = "Mã tra cứu" trên cổng = TTKhac/ma_tra_cuu của HĐ.

Quy trình (khảo sát 27/10/2026):
  1. GET  /                     → form TT78 (key + captcha)
  2. GET  /Captcha/Show         → PNG 120x38, 4 chữ số, gắn cookie phiên → model g_120x38
  3. POST /Invoice/SearchTaxInv  key, captch
       → captcha SAI: trang chủ dựng lại, ô key bị xóa trắng (value="")
       → captcha ĐÚNG: ô key được giữ lại (value="<key>")
         · thấy HĐ: HTML có nút gọi downloadTaxInv(<id>) / downloadInv(<id>)
         · không thấy: không có nút tải (thông báo đẩy qua TempData, hiện client-side)
  4. GET  /Invoice/DownloadTaxInv?id=<id>  (TT78) | /Invoice/Download?id=<id>  (TT32)
       → ZIP chứa 1 PDF bản thể hiện (có số HĐ)

Trang không có reCAPTCHA. Đã kiểm 27/10/2026: captcha g_120x38 server nhận 10/10
(ô key được giữ lại = captcha qua; captcha sai → ô key bị xóa). CHƯA có HĐ Megabiz thật
trong hệ thống nên bước 4 (tải PDF) chưa chạy thật, và cách dựng trang kết quả tìm-thấy
chưa quan sát trực tiếp — PDF luôn qua check_pdf_is_invoice nên sai cũng không lưu nhầm.
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request, solve_captcha

BASE = "https://tracuu.vinvoice.vn"
CAPTCHA_GROUP = "g_120x38"
CAPTCHA_TRIES = 4

# downloadTaxInv('123') / downloadInv(123) — KHÔNG khớp định nghĩa hàm (tham số là tên 'invid')
_DL_RE = re.compile(r"(downloadTaxInv|downloadInv)\(\s*['\"]?(\d+)['\"]?\s*\)")
_DL_ENDPOINT = {"downloadTaxInv": "/Invoice/DownloadTaxInv", "downloadInv": "/Invoice/Download"}
_KEY_RE = re.compile(r"id='key'[^>]*value=\"([^\"]*)\"|value=\"([^\"]*)\"[^>]*id='key'")


class _WrongCaptcha(Exception):
    pass


class MegabizAdapter(PdfAdapter):
    source_id = "megabiz"
    name = "Megabiz (vinvoice)"
    mst_tcgp = ("0105844836",)
    hosts = ("tracuu.vinvoice.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        host = (urlsplit(url).hostname or "").lower()
        if host != "tracuu.vinvoice.vn":
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
            raise PdfFetchError("XML không có mã tra cứu Megabiz", "no_code")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            s = new_session()
            try:
                func, inv_id = self._search(s, code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"Megabiz từ chối captcha {CAPTCHA_TRIES} lần", "captcha")
        z = request(s, "GET", BASE + _DL_ENDPOINT[func], params={"id": inv_id},
                    headers={"Referer": f"{BASE}/"})
        try:
            with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
                name = next((n for n in zf.namelist() if n.lower().endswith(".pdf")), None)
                if name is None:
                    raise PdfFetchError("ZIP Megabiz không có file PDF", "no_pdf")
                pdf = zf.read(name)
        except zipfile.BadZipFile as e:
            raise PdfFetchError(f"Megabiz trả ZIP hỏng: {e}", "bad_content") from e
        if not is_pdf(pdf):
            raise PdfFetchError("File trong ZIP Megabiz không phải PDF hợp lệ", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def _search(self, s, code: str):
        """→ (tên hàm tải, id). Captcha gắn cookie phiên."""
        request(s, "GET", f"{BASE}/")
        img = request(s, "GET", f"{BASE}/Captcha/Show", headers={"Referer": f"{BASE}/"})
        if img.status_code != 200 or not img.content:
            raise PdfFetchError("Không lấy được captcha Megabiz", "server")
        answer = solve_captcha(img.content, CAPTCHA_GROUP)
        r = request(s, "POST", f"{BASE}/Invoice/SearchTaxInv",
                    data={"key": code, "captch": answer}, headers={"Referer": f"{BASE}/", "Origin": BASE})
        page = html.unescape(r.text)
        m = _DL_RE.search(page)
        if m:
            return m.group(1), m.group(2)
        # captcha đúng thì ô key được giữ lại; bị xóa trắng = captcha sai → thử lại
        km = _KEY_RE.search(r.text)
        echoed = next((g for g in km.groups() if g is not None), "") if km else ""
        if echoed.strip() != code:
            raise _WrongCaptcha("captcha sai (ô key bị xóa)")
        raise PdfFetchError(f"Megabiz không tìm thấy HĐ với mã {code}", "no_pdf")


register(MegabizAdapter())
