"""
VININVOICE — MSTTCGP 0109282176. Tra cứu: https://tracuu.vininvoice.vn/#/
Cùng phần mềm với MS-INVOICE (tracuu.myinvoice.vn, xem myinvoice.py), khác host.

Không captcha, không cookie. Cổng là SPA React gọi REST:
    GET /erp/rest/s1/iam-entry/invoices/<mã>/header-info  → {"invoiceId","invoiceNo","invoiceTotal",...}
    GET /erp/rest/s1/iam-entry/invoices/<mã>/pdf          → 200 application/pdf (Apache FOP)
    GET /erp/rest/s1/iam-entry/invoices/<mã>/xml          → 200 text/xml (XML có chữ ký)
        mã sai → 400 {"errors": "Không tìm thấy hóa đơn ..."}

Mã tra cứu: XML của VININVOICE không có TTKhac; mã 16 ký tự (vd 94C2359A44624334) chỉ in trên PDF /
mail. Như MS-INVOICE, cổng nhận thẳng MCCQT (mã CQT 34 ký tự) → tra được từ dữ liệu Thuế.

Kiểm 26/10/2026: 3/3 HĐ thật (C24TLP-14 trong DB + C23TLP-10, C23TAA-58 chỉ có trong mail) tải bằng
MCCQT; mã in trên PDF cho cùng HĐ. PDF trùng chữ với PDF trong mail.
"""
from __future__ import annotations

import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

BASE = "https://tracuu.vininvoice.vn"
API = f"{BASE}/erp/rest/s1/iam-entry/invoices"
_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")


class VinInvoiceAdapter(PdfAdapter):
    source_id = "vininvoice"
    name = "VININVOICE (tracuu.vininvoice.vn)"
    mst_tcgp = ("0109282176",)
    hosts = ("tracuu.vininvoice.vn",)

    @staticmethod
    def lookup_codes(invoice: dict) -> list:
        """Mã tra cứu riêng (nếu có) trước, rồi MCCQT; bỏ trùng/rỗng/sai dạng (mã nằm trong URL)."""
        tt_khac = invoice.get("tt_khac") or {}
        cands = (invoice.get("ma_tra_cuu"), tt_khac.get("Mã tra cứu hóa đơn"), tt_khac.get("MTCuu"),
                 invoice.get("ma_cqt"))
        codes = (str(c).strip() for c in cands if c and str(c).strip())
        return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]

    def _get(self, invoice: dict, kind: str):
        codes = self.lookup_codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu lẫn MCCQT (VININVOICE)", "no_code")
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{API}/{code}/{kind}", headers={"Referer": f"{BASE}/"})
            if r.status_code == 400:                    # không tìm thấy → thử mã kế
                last = self._error_text(r) or f"không tìm thấy mã {code}"
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"VININVOICE trả HTTP {r.status_code}", "other")
            return r
        raise PdfFetchError(f"VININVOICE không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        r = self._get(invoice, "pdf")
        if not is_pdf(r.content):
            raise PdfFetchError(f"VININVOICE trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        r = self._get(invoice, "xml")
        xml = r.content or b""
        head = xml.lstrip()[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError("VININVOICE trả nội dung không phải XML", "bad_content")
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
        """Mail VININVOICE chỉ có link trang chủ; dùng được khi mail có kèm mã tra cứu. Không mở link."""
        secret = (code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu hóa đơn": secret}}


register(VinInvoiceAdapter())
