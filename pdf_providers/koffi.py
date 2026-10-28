"""
Koffi / KoffiOutbound (CTCP ZAMO) — MSTTCGP 0313950909.
Tra cứu: https://koffi.vn/outbound/lookup-invoice  (1 ô "Mã hoá đơn" — mã trong email, không captcha).

Trang Next.js gọi thẳng API công khai (đọc bundle 27/10/2026):

    GET https://api.koffi.vn/invoices/code/<mã>?format=pdf|xml
      → 200 JSON {status, url, download_url}   : status "DONE" → url = xem, download_url = tải file
                                                  "BUILDING"/"READY" = đang dựng file, "FAIL" = lỗi NCC
      → 404 JSON {"code":"E500","message":"[E500] No entity tenant found with id = …"} : mã sai → 'no_pdf'
        (mã được giải ra id tenant — mã bịa cho ra id lạ)
Trang cũng nhận mã trên đường dẫn: /outbound/lookup-invoice/<mã> (useParams().code).

Mã: chỉ có trong mail Koffi gửi / TTKhac nếu người bán ghi vào XML.
Chưa có HĐ thật (DB 27/10: 0 HĐ MSTTCGP 0313950909, 0 link mail) — chỉ kiểm phản hồi "không tìm thấy";
nhánh thành công dựng theo bundle JS, cần kiểm lại khi có HĐ thật.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .formlookup import lookup_code

API = "https://api.koffi.vn/invoices/code"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_LINK_RE = re.compile(r"/lookup-invoice/([^/?#]+)")


class KoffiAdapter(PdfAdapter):
    source_id = "koffi"
    name = "Koffi (ZAMO, koffi.vn)"
    mst_tcgp = ("0313950909",)
    hosts = ("koffi.vn",)

    def _file(self, invoice: dict, fmt: str):
        code = lookup_code(invoice)
        if not _CODE_RE.match(code):
            raise PdfFetchError("Không có mã hóa đơn Koffi (mã chỉ có trong mail)", "no_code")
        s = new_session()
        r = request(s, "GET", f"{API}/{code}", params={"format": fmt},
                    headers={"Origin": "https://koffi.vn", "Referer": "https://koffi.vn/"})
        if r.status_code == 404:
            raise PdfFetchError(f"Koffi không tìm thấy HĐ mã {code}", "no_pdf")
        try:
            js = r.json() if r.status_code == 200 else None
        except ValueError:
            js = None
        if not isinstance(js, dict) or "code" in js:
            raise PdfFetchError(f"Koffi tra mã {code}: HTTP {r.status_code}, không phải kết quả HĐ", "no_pdf")
        status = str(js.get("status") or "")
        if status == "NOT_FOUND":
            raise PdfFetchError(f"Koffi không tìm thấy HĐ mã {code}", "no_pdf")
        if status != "DONE":
            raise PdfFetchError(f"Koffi chưa dựng xong file (status={status or '?'})", "server")
        url = js.get("download_url") or js.get("url")
        if not url:
            raise PdfFetchError("Koffi trả DONE nhưng không có download_url", "bad_content")
        r2 = request(s, "GET", url, headers={"Referer": "https://koffi.vn/"})
        if r2.status_code != 200:
            raise PdfFetchError(f"Koffi tải file trả HTTP {r2.status_code}", "other")
        return r2

    def fetch_pdf(self, invoice: dict) -> bytes:
        r = self._file(invoice, "pdf")
        if not is_pdf(r.content):
            raise PdfFetchError(f"Koffi trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        xml = self._file(invoice, "xml").content or b""
        head = xml.lstrip()[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError("Koffi trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """koffi.vn/outbound/lookup-invoice/<mã> (hoặc ?code=) / mã trong mail. Không mở link."""
        secret = ""
        m = _LINK_RE.search(urlsplit(url).path or "")
        if m:
            secret = unquote(m.group(1)).strip()
        secret = secret or link_params(url).get("code") or (code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã hóa đơn": secret}}


register(KoffiAdapter())
