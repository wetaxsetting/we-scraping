"""
WeTax (wetax.com.vn) — phần mềm HĐĐT của chính Webcash; người bán dùng WeTax gửi mail
"[<tên người bán>][1][C26MSC][132385]Thông báo phát hành HĐĐT" (vd Ly Thái Bakery).

Mail có 2 link tải thẳng (khảo sát 27/10/2026, user đã kiểm tay):
  GET https://api.wetax.com.vn/api/wtx/ca/v1/download/pdf{,2}/<id>/<hash 32 hex>  → application/pdf
  GET https://api.wetax.com.vn/api/wtx/ca/v1/download/xml{,2}/<id>/<hash>          → XML (HDon)
  hash sai → 404 rỗng. Không captcha, không cần đăng nhập.
Link "https://wetax.com.vn/tra-cuu?lookUpCode=..." cùng mail là form tra (captcha 160x60) → bỏ qua,
link tải thẳng ở trên đã đủ.
XML Thuế không có <id>/<hash> → adapter chỉ chạy qua link trong mail; PDF/XML khớp HĐ theo nội dung.
Thật 27/10: Ly Thái Bakery C25MSA-34792 (link v1) và C26MSC-132385 (link v2) — PDF + XML.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

API = "https://api.wetax.com.vn/api/wtx/ca/v1/download"
_PATH_RE = re.compile(r"^/api/wtx/ca/v1/download/(?:pdf|xml)(2?)/(\d+)/([0-9a-f]{32})/?$", re.I)


class WetaxAdapter(PdfAdapter):
    source_id = "wetax"
    name = "WeTax (Webcash)"
    mst_tcgp = ()                      # XML không có khóa tải → không định tuyến theo MSTTCGP
    hosts = ("wetax.com.vn",)          # gồm api.wetax.com.vn

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Chỉ nhận link tải thẳng pdf/xml của api.wetax.com.vn (đọc id + hash trên đường dẫn)."""
        parts = urlsplit(url)
        m = _PATH_RE.match(parts.path or "")
        if (parts.hostname or "").lower() != "api.wetax.com.vn" or not m:
            return None
        return {"wetax_ver": m.group(1), "wetax_id": m.group(2), "wetax_hash": m.group(3).lower()}

    def _get(self, invoice: dict, kind: str) -> bytes:
        if not invoice.get("wetax_id") or not invoice.get("wetax_hash"):
            raise PdfFetchError("Không có link tải WeTax (chỉ có trong mail người bán)", "no_code")
        url = f"{API}/{kind}{invoice.get('wetax_ver') or ''}/{invoice['wetax_id']}/{invoice['wetax_hash']}"
        r = request(new_session(), "GET", url)
        if r.status_code == 404:
            raise PdfFetchError(f"WeTax không còn file {kind} cho link này (404)", "no_pdf")
        return r.content or b""

    def fetch_pdf(self, invoice: dict) -> bytes:
        data = self._get(invoice, "pdf")
        if not is_pdf(data):
            raise PdfFetchError("WeTax trả nội dung không phải PDF", "bad_content")
        check_pdf_is_invoice(data, invoice)
        return data

    def fetch_xml(self, invoice: dict) -> bytes:
        data = self._get(invoice, "xml")
        if b"<HDon" not in data[:2000]:
            raise PdfFetchError("WeTax trả nội dung không phải XML hóa đơn", "bad_content")
        return data


register(WetaxAdapter())
