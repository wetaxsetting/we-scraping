# pdf_providers/ameinvoice.py
"""
AMEINVOICE (NC9 Việt Nam, ameinvoice.vn) — MSTTCGP 0312270160.

Không captcha (trang có nạp reCAPTCHA của plugin Contact Form 7, chỉ dùng cho form liên hệ).
XML Thuế có sẵn link tra cứu trong `lookup_url`:
    https://ameinvoice.vn/einvoice/?key_receive=<20 số>
Trang đó có nút "Download PDF" trỏ thẳng:
    GET /view-teamplate-invoice/?down_pdf=true&key_receive=<mã>
        → 200 application/pdf (wkhtmltopdf), có BOM UTF-8 đứng trước "%PDF"
        → 200 "Connection Failed: 1049" khi máy chủ NCC mất CSDL của người bán đó (→ kind `server`)
`down_xml=true` trả trang HTML, không phải XML → không dùng.
Link mail `einvoice/?key_receive=..&confirm_key=..&is_confirm=1` chỉ đọc `key_receive`, không mở link.

Thật 03/11/2026: C23TVN-424 (3702473210) và C24TLB-240 (0314391594) tải được; C23TLB-128
(0314391594) máy chủ trả "Connection Failed: 1049".
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://ameinvoice.vn"
PDF_URL = f"{BASE}/view-teamplate-invoice/"
_KEY_RE = re.compile(r"^\d{10,30}$")


class AmeInvoiceAdapter(PdfAdapter):
    source_id = "ameinvoice"
    name = "AMEINVOICE (NC9 Việt Nam)"
    mst_tcgp = ("0312270160",)
    hosts = ("ameinvoice.vn",)

    @staticmethod
    def lookup_key(invoice: dict) -> str:
        """key_receive từ lookup_url, không có thì từ mã tra cứu đã lưu."""
        tt_khac = invoice.get("tt_khac") or {}
        for url in (invoice.get("lookup_url"), tt_khac.get("PortalLink")):
            key = link_params(url or "").get("key_receive", "")
            if _KEY_RE.match(key):
                return key
        key = str(invoice.get("ma_tra_cuu") or "").strip()
        return key if _KEY_RE.match(key) else ""

    def invoice_from_link(self, url: str, code, mail_text: str):
        host = (urlsplit(url or "").hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        key = link_params(url).get("key_receive", "")
        if not _KEY_RE.match(key):
            return None
        return {"ma_tra_cuu": key, "lookup_url": f"{BASE}/einvoice/?key_receive={key}"}

    def fetch_pdf(self, invoice: dict) -> bytes:
        key = self.lookup_key(invoice)
        if not key:
            raise PdfFetchError("Không có key_receive AMEINVOICE (lookup_url trong XML)", "no_code")
        s = new_session()
        r = request(s, "GET", PDF_URL, params={"down_pdf": "true", "key_receive": key},
                    headers={"Referer": f"{BASE}/einvoice/?key_receive={key}"})
        data = r.content or b""
        i = data.find(b"%PDF-", 0, 16)                  # bỏ BOM / dòng trống đứng trước
        pdf = data[i:] if i >= 0 else b""
        if r.status_code != 200 or not is_pdf(pdf):
            head = data[:120].decode("utf-8", "replace").strip("﻿\r\n ")
            if "Connection Failed" in head:
                raise PdfFetchError(f"AMEINVOICE lỗi CSDL phía NCC ({head})", "server")
            raise PdfFetchError(
                f"AMEINVOICE trả nội dung không phải PDF (HTTP {r.status_code}, "
                f"{r.headers.get('Content-Type')}): {head[:60]!r}", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf


register(AmeInvoiceAdapter())
