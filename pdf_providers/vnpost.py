"""
Tổng Công ty Bưu điện Việt Nam (VNPost) — MSTTCGP 0102595740. Tra cứu: https://tracuuhoadon.vnpost.vn/

SPA Angular; cấu hình API ở /assets/environments/prod.json (javaEndpoint). Khảo sát 27/10/2026:
"Mã xác thực" 4 chữ số là số ngẫu nhiên sinh ở trình duyệt (Math.floor(1e3+9e3*Math.random())),
so khớp cũng ở trình duyệt — server KHÔNG nhận captcha nào. prod.json có khai key reCAPTCHA nhưng
luồng tra HĐ không dùng. Không cookie.
    GET /invoice-lookup/api/invoices/lookup?key=<mã>
        → {"success":true,"data":[{number, symbol, html, xml, ...}], "totalRecord":1}
        → 404 {"success":false,"message":"Không tìm thấy hóa đơn cần tra cứu, vui lòng kiểm tra và thử lại."}
    GET /invoice-lookup/api/invoices/<mã>/pdf        → PDF (nút "In")
        → 404 {"message":"Không tìm thấy hóa đơn, vui lòng kiểm tra và thử lại."}
    GET /invoice-lookup/api/invoices/lookup/zip-download?key=<mã>   → ZIP (không dùng)
XML lấy từ trường data[0].xml của lookup. Link mail: ?token=<JWT>, payload có "invoiceKey" = mã
(trang tự giải JWT ở trình duyệt, không kiểm chữ ký) → đọc payload, không mở link.

Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .lookup_codes import lookup_codes

BASE = "https://tracuuhoadon.vnpost.vn"
API = f"{BASE}/invoice-lookup/api/invoices"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": f"{BASE}/"}


def jwt_invoice_key(token: str) -> str:
    """invoiceKey trong payload JWT (không kiểm chữ ký — chỉ đọc). Lỗi → ''."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
        return str(payload.get("invoiceKey") or "").strip()
    except (IndexError, ValueError, UnicodeDecodeError, AttributeError):
        return ""


class VnPostAdapter(PdfAdapter):
    source_id = "vnpost"
    name = "VNPost (tracuuhoadon.vnpost.vn)"
    mst_tcgp = ("0102595740",)
    hosts = ("tracuuhoadon.vnpost.vn",)

    @staticmethod
    def codes(invoice: dict) -> list:
        return lookup_codes(invoice, ("Mã tra cứu", "Mã tra cứu hóa đơn", "MTCuu", "invoiceKey"), _CODE_RE)

    def _each_code(self, invoice: dict):
        codes = self.codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu (VNPost)", "no_code")
        return codes

    def fetch_pdf(self, invoice: dict) -> bytes:
        codes = self._each_code(invoice)
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{API}/{quote(code, safe='')}/pdf", headers=HEADERS)
            if r.status_code == 404:
                last = self._message(r)
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"VNPost trả HTTP {r.status_code}: {self._message(r)}", "other")
            if not is_pdf(r.content):
                raise PdfFetchError(f"VNPost trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                                    "bad_content")
            check_pdf_is_invoice(r.content, invoice)
            return r.content
        raise PdfFetchError(f"VNPost không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    def fetch_xml(self, invoice: dict) -> bytes:
        codes = self._each_code(invoice)
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{API}/lookup", params={"key": code}, headers=HEADERS)
            try:
                js = r.json() or {}
            except ValueError:
                raise PdfFetchError(f"VNPost trả HTTP {r.status_code} không phải JSON", "other")
            rows = js.get("data") or []
            if r.status_code == 404 or not rows:
                last = str(js.get("message") or "")
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"VNPost trả HTTP {r.status_code}", "other")
            xml = str(rows[0].get("xml") or "").strip().encode("utf-8")
            if b"<HDon" not in xml[:5000]:
                raise PdfFetchError(f"VNPost không có XML cho mã {code}", "bad_content")
            shdon = str(invoice.get("shdon") or "").strip()
            m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
            if shdon and (not m or int(m.group(1)) != int(shdon)):
                raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
            return xml
        raise PdfFetchError(f"VNPost không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    @staticmethod
    def _message(r) -> str:
        try:
            return str((r.json() or {}).get("message") or "").strip()
        except ValueError:
            return ""

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link ?token=<JWT có invoiceKey> hoặc mã trong mail. Không mở link."""
        p = link_params(url)
        secret = jwt_invoice_key(p.get("token", "")) or p.get("key", "") or (code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


register(VnPostAdapter())
