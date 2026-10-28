# pdf_providers/homecasta.py
"""
Home Casta Invoice — MSTTCGP 0201802839. Tra cứu: https://tracuu.homecasta.vn/#/

Cùng phần mềm với VININVOICE / MS-INVOICE (xem vininvoice.py, myinvoice.py): SPA React gọi REST
iam-entry, chỉ khác host và tiền tố (`/api/rest/s1/` thay cho `/erp/rest/s1/`). Không captcha, không cookie:
    GET /api/rest/s1/iam-entry/invoices/<mã>/header-info  → {"invoiceId","invoiceNo",...}
    GET /api/rest/s1/iam-entry/invoices/<mã>/pdf          → PDF
    GET /api/rest/s1/iam-entry/invoices/<mã>/xml          → XML
        mã sai → 400 {"errorCode":400,"errors":"Không tìm thấy hóa đơn có mã tra cứu: ...\n"}

Mã tra cứu: mã riêng (ma_tra_cuu / TTKhac) trước, rồi MCCQT (cùng phần mềm với 2 cổng trên vốn nhận MCCQT —
với Home Casta chưa kiểm được).

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy (400 JSON như trên, cả /header-info và /pdf).
"""
from __future__ import annotations

from . import register
from .base import PdfFetchError
from .common import new_session, request
from .vininvoice import VinInvoiceAdapter

BASE = "https://tracuu.homecasta.vn"
API = f"{BASE}/api/rest/s1/iam-entry/invoices"


class HomeCastaAdapter(VinInvoiceAdapter):
    source_id = "homecasta"
    name = "Home Casta Invoice (tracuu.homecasta.vn)"
    mst_tcgp = ("0201802839",)
    hosts = ("tracuu.homecasta.vn",)

    def _get(self, invoice: dict, kind: str):
        codes = self.lookup_codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu lẫn MCCQT (Home Casta)", "no_code")
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{API}/{code}/{kind}", headers={"Referer": f"{BASE}/"})
            if r.status_code in (400, 404):              # không tìm thấy → thử mã kế
                last = self._error_text(r) or f"không tìm thấy mã {code}"
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"Home Casta trả HTTP {r.status_code}", "other")
            return r
        raise PdfFetchError(f"Home Casta không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")


register(HomeCastaAdapter())
