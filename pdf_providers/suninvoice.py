"""
SUN Invoice — MSTTCGP 0111488368. Tra cứu: https://tracuu.suninvoice.vn/

Cùng phần mềm (Moqui "iam-entry") với MS-INVOICE / VININVOICE (myinvoice.py, vininvoice.py), khác
host và tiền tố đường dẫn (/api/rest/s1 thay cho /erp/rest/s1). Không captcha, không cookie, không
reCAPTCHA (khảo sát 27/10/2026, đọc /static/js/main.*.js):
    GET /api/rest/s1/iam-entry/invoices/<mã>/header-info
        → 400 {"errorCode":400,"errors":"Không tìm thấy hóa đơn có mã tra cứu: <mã>"}
    GET /api/rest/s1/iam-entry/invoices/<mã>/pdf
        → 400 {"errorCode":400,"errors":"Không tìm thấy hóa đơn hoặc hóa đơn đã bị xóa"}
    GET /api/rest/s1/iam-entry/invoices/<mã>/xml

Mã: mã tra cứu (TTKhac) trước; như 2 cổng cùng phần mềm, thử thêm MCCQT (CHƯA kiểm ở cổng này).
Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy.
"""
from __future__ import annotations

import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request
from .lookup_codes import lookup_codes

BASE = "https://tracuu.suninvoice.vn"
API = f"{BASE}/api/rest/s1/iam-entry/invoices"
_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")


class SunInvoiceAdapter(PdfAdapter):
    source_id = "suninvoice"
    name = "SUN Invoice (tracuu.suninvoice.vn)"
    mst_tcgp = ("0111488368",)
    hosts = ("tracuu.suninvoice.vn",)

    @staticmethod
    def codes(invoice: dict) -> list:
        codes = lookup_codes(invoice, ("Mã tra cứu hóa đơn", "Mã tra cứu", "MTCuu", "MaTraCuu"), _CODE_RE)
        mccqt = str(invoice.get("ma_cqt") or "").strip()
        if _CODE_RE.match(mccqt) and mccqt not in codes:
            codes.append(mccqt)
        return codes

    def _get(self, invoice: dict, kind: str):
        codes = self.codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu lẫn MCCQT (SUN Invoice)", "no_code")
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{API}/{code}/{kind}", headers={"Referer": f"{BASE}/"})
            if r.status_code == 400:
                last = self._error_text(r) or f"không tìm thấy mã {code}"
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"SUN Invoice trả HTTP {r.status_code}", "other")
            return r
        raise PdfFetchError(f"SUN Invoice không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        r = self._get(invoice, "pdf")
        if not is_pdf(r.content):
            raise PdfFetchError(f"SUN Invoice trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        r = self._get(invoice, "xml")
        xml = r.content or b""
        head = xml.lstrip()[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError("SUN Invoice trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    @staticmethod
    def _error_text(r) -> str:
        try:
            return str((r.json() or {}).get("errors") or "").strip()
        except ValueError:
            return ""

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail chỉ dùng được khi có kèm mã tra cứu. Không mở link."""
        secret = (code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu hóa đơn": secret}}


register(SunInvoiceAdapter())
