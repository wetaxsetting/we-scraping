"""
SaigonInvoice (Công ty Kế toán và dịch vụ eHCM) — MSTTCGP 0310926922.
Tra cứu: https://invoice.ehcm.vn/#TraCuu  (1 ô "Nhập mã tra cứu", không captcha).

ASP.NET WebForms trên trang chủ. Khảo sát 27/10/2026:

    GET  /  → lấy __VIEWSTATE / __VIEWSTATEGENERATOR / __EVENTVALIDATION
    POST /  txtSearch=<mã>, btnSearch="Tra cứu" + các ô ẩn
      → 200 HTML có window.alert('Không có mã tra cứu này. …')   : không thấy → 'no_pdf'
      → thành công: CHƯA THẤY. Adapter nhận PDF trả thẳng, hoặc 1 link PDF cùng host trong trang;
        khác đi thì 'bad_content' để khảo sát lại.

Mã: TTKhac mã tra cứu / ma_tra_cuu.
Chưa có HĐ thật (DB 27/10: 0 HĐ MSTTCGP 0310926922, 0 link mail) — chỉ kiểm phản hồi "không tìm thấy".
"""
from __future__ import annotations

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import new_session, request
from .formlookup import hidden_fields, lookup_code, pdf_from_response

URL = "https://invoice.ehcm.vn/"
NOT_FOUND = "Không có mã tra cứu này"


class EhcmAdapter(PdfAdapter):
    source_id = "ehcm"
    name = "SaigonInvoice / eHCM (invoice.ehcm.vn)"
    mst_tcgp = ("0310926922",)
    hosts = ("invoice.ehcm.vn",)

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có mã tra cứu (eHCM)", "no_code")
        s = new_session()
        r = request(s, "GET", URL)
        if r.status_code != 200:
            raise PdfFetchError(f"eHCM trả HTTP {r.status_code}", "other")
        form = hidden_fields(r.text)
        if "__VIEWSTATE" not in form:
            raise PdfFetchError("eHCM: trang không có __VIEWSTATE (đổi giao diện?)", "bad_content")
        form.update({"txtSearch": code, "btnSearch": "Tra cứu"})
        r = request(s, "POST", URL, data=form, headers={"Referer": URL})
        if r.status_code != 200:
            raise PdfFetchError(f"eHCM tra cứu trả HTTP {r.status_code}", "other")
        if NOT_FOUND in (r.text or ""):
            raise PdfFetchError(f"eHCM không tìm thấy HĐ mã {code}", "no_pdf")
        return pdf_from_response(s, r, invoice, self.name)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Cổng chỉ cần mã → dùng được khi mail kèm mã tra cứu. Không mở link."""
        secret = (code or "").strip()
        if not secret:
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


register(EhcmAdapter())
