"""
W-INVOICE / Tư vấn – Thương mại Khánh Linh — MSTTCGP 0307633556. Tra cứu: https://winvoice.vn/tra-cuu
(Không liên quan WinInvoice/Win Tech của wininvoice.py.)

Khảo sát 27/10/2026 (script trong trang /tra-cuu). Không captcha ở form tra cứu: ô "Tôi không phải
là người máy" chỉ là widget tự vẽ (checkbox + chữ "reCAPTCHA") của form ĐĂNG KÝ TƯ VẤN, không nạp
script Google, không dính form tra cứu. Không cookie.
    POST /api/lookup   multipart lookup_code=<mã>
        → {"success":true,"invoice":{lookup_code, seller_tax_code, symbol_code, invoice_number, ...}}
        → {"success":false,"message":"Không tìm thấy hóa đơn hoặc sai mã tra cứu..."}
    GET  /winvoice/download/<mã>?doc_type=pdf&download=1   → application/pdf (Chromium/Skia)
    GET  /winvoice/download/<mã>?doc_type=xml&download=1   → XML (bản trên cổng KHÔNG có chữ ký số)
        mã sai → 404 text "Lỗi kết nối portal: ..." → luôn tra /api/lookup trước để phân biệt.
Mã: TTKhac "Mã tra cứu hóa đơn" (TTin Id="privateCode"), 32 hex.

Đã kiểm 27/10/2026 bằng mã ví dụ in sẵn trên trang (B6A8F5FE…, HKD Phạm Thị Lịch C26MTL-571):
lookup + PDF (65 KB, có số HĐ) + XML đều lấy được. Chưa có HĐ thật của công ty.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .lookup_codes import lookup_codes

BASE = "https://winvoice.vn"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": BASE, "Referer": f"{BASE}/tra-cuu"}


class WInvoiceAdapter(PdfAdapter):
    source_id = "winvoice"
    name = "W-INVOICE Khánh Linh (winvoice.vn)"
    mst_tcgp = ("0307633556",)
    hosts = ("winvoice.vn",)

    @staticmethod
    def codes(invoice: dict) -> list:
        return lookup_codes(invoice, ("Mã tra cứu hóa đơn", "Mã tra cứu", "privateCode"), _CODE_RE)

    def _lookup(self, s, invoice: dict) -> str:
        codes = self.codes(invoice)
        if not codes:
            raise PdfFetchError("XML không có 'Mã tra cứu hóa đơn' (W-INVOICE)", "no_code")
        last = ""
        for code in codes:
            r = request(s, "POST", f"{BASE}/api/lookup", files={"lookup_code": (None, code)}, headers=HEADERS)
            try:
                js = r.json() or {}
            except ValueError:
                raise PdfFetchError(f"W-INVOICE trả HTTP {r.status_code} không phải JSON", "other")
            if js.get("success") and js.get("invoice"):
                return code
            last = str(js.get("message") or "")
        raise PdfFetchError(f"W-INVOICE không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    def _download(self, invoice: dict, doc_type: str):
        s = new_session()
        code = self._lookup(s, invoice)
        r = request(s, "GET", f"{BASE}/winvoice/download/{quote(code, safe='')}",
                    params={"doc_type": doc_type, "download": 1}, headers={"Referer": f"{BASE}/tra-cuu"})
        if r.status_code != 200:
            raise PdfFetchError(f"W-INVOICE tải {doc_type} trả HTTP {r.status_code}: {r.text[:150]}", "no_pdf")
        return r

    def fetch_pdf(self, invoice: dict) -> bytes:
        r = self._download(invoice, "pdf")
        if not is_pdf(r.content):
            raise PdfFetchError(f"W-INVOICE trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        xml = self._download(invoice, "xml").content or b""
        if b"<HDon" not in xml[:5000]:
            raise PdfFetchError("W-INVOICE trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link /tra-cuu?lookup_code=<mã> hoặc mã trong mail. Không mở link."""
        p = link_params(url)
        secret = (p.get("lookup_code") or p.get("code") or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu hóa đơn": secret}}


register(WInvoiceAdapter())
