# pdf_providers/myinvoice.py
"""
MS-INVOICE (My Software, myinvoice.vn) — MSTTCGP 0108971656. Tra cứu: https://tracuu.myinvoice.vn/#/

Không captcha, không cookie. Cổng là SPA React gọi REST:
    GET /erp/rest/s1/iam-entry/invoices/<mã tra cứu>/header-info
        → 200 {"invoiceId","invoiceNo","invoiceTotal",...} | 400 {"errors": "Không tìm thấy hóa đơn có mã tra cứu: ..."}
    GET /erp/rest/s1/iam-entry/invoices/<mã tra cứu>/pdf
        → 200 application/pdf (Apache FOP)             : thành công
        → 400 {"errors": "Không tìm thấy hóa đơn hoặc hóa đơn đã bị xóa"}

Mã tra cứu: XML của MS-INVOICE KHÔNG có TTKhac nào, nhưng cổng nhận thẳng MCCQT (mã CQT
34 ký tự) làm mã tra cứu. Có mã tra cứu riêng (ma_tra_cuu / TTKhac) thì thử trước.

Khảo sát 10/2026 trên HĐ thật C26TPF-18326 (PMP Fine Foods): tải thẳng /pdf bằng MCCQT.
"""
from __future__ import annotations

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

BASE = "https://tracuu.myinvoice.vn"
API = f"{BASE}/erp/rest/s1/iam-entry/invoices"


class MyInvoiceAdapter(PdfAdapter):
    source_id = "myinvoice"
    name = "MS-INVOICE (My Software)"
    mst_tcgp = ("0108971656",)

    @staticmethod
    def lookup_codes(invoice: dict) -> list:
        """Mã tra cứu riêng (nếu có) trước, rồi MCCQT; bỏ trùng/rỗng."""
        tt_khac = invoice.get("tt_khac") or {}
        cands = (tt_khac.get("MTCuu"), tt_khac.get("MaTraCuu"), invoice.get("ma_tra_cuu"),
                 invoice.get("ma_cqt"))
        return list(dict.fromkeys(str(c).strip() for c in cands if c and str(c).strip()))

    def fetch_pdf(self, invoice: dict) -> bytes:
        codes = self.lookup_codes(invoice)
        if not codes:
            raise PdfFetchError("XML không có mã tra cứu lẫn MCCQT (MS-INVOICE)", "no_code")
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{API}/{code}/pdf", headers={"Referer": f"{BASE}/"})
            if r.status_code == 400:                    # không tìm thấy → thử mã kế
                last = self._error_text(r) or f"không tìm thấy mã {code}"
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"MS-INVOICE trả HTTP {r.status_code}", "other")
            if not is_pdf(r.content):
                raise PdfFetchError(
                    f"MS-INVOICE trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                    "bad_content")
            check_pdf_is_invoice(r.content, invoice)
            return r.content
        raise PdfFetchError(f"MS-INVOICE không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    @staticmethod
    def _error_text(r) -> str:
        try:
            return str(r.json().get("errors") or "").strip()
        except (ValueError, AttributeError):
            return ""


register(MyInvoiceAdapter())
