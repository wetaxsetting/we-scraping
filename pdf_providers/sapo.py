"""
Sapo Invoice — MSTTCGP 0103243195. Tra cứu: https://www.sapo.vn/tra-cuu-sapo-invoice.html
(ô "Nhập mã tra cứu hóa đơn"; link trong mail/QR dạng `...tra-cuu-sapo-invoice.html?qrcode=<mã>`).

Khảo sát 27/10/2026 (đọc script trên trang + gửi 2 mã bịa). Trang gọi thẳng API, không captcha,
không cookie (Cloudflare phía trước nhưng không chặn request thường):
    POST https://invoice.sapo.vn/api/invoices/look_up  JSON {"invoice": {"reference_code": <mã>}}
      → 200 {"invoice": {"base64": "<PDF base64>", ...}}                               : thành công
      → 404 {"errors":[{"code":"not_found","message":"Không tìm thấy thông tin hóa đơn ..."}]} : 'no_pdf'
      → 200 {"errors":[{"fields":["publish_status_waiting_for_response" | ..._rejected | ...]}]}
         HĐ chưa được CQT cấp mã / bị từ chối → 'no_pdf' (chờ lượt sau)
Cùng API nhận {"invoice": {"base64_xml": ...}} (tra bằng file XML) — adapter không dùng.
Không có endpoint tải XML.

Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy. Mã tra cứu: ma_tra_cuu / TTKhac kiểu "mã tra cứu".
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

API = "https://invoice.sapo.vn/api/invoices/look_up"
PAGE = "https://www.sapo.vn/tra-cuu-sapo-invoice.html"
_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
_KEY_HINTS = ("tracuu", "baomat", "mtcuu", "matc", "lookup", "searchkey", "reference")


def _norm_key(key: str) -> str:
    s = unicodedata.normalize("NFD", str(key)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def lookup_codes(invoice: dict) -> list:
    tt_khac = invoice.get("tt_khac") or {}
    cands = [invoice.get("ma_tra_cuu")]
    cands += [v for k, v in tt_khac.items() if any(h in _norm_key(k) for h in _KEY_HINTS)]
    codes = (str(c).strip() for c in cands if c and str(c).strip())
    return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]


class SapoAdapter(PdfAdapter):
    source_id = "sapo"
    name = "Sapo Invoice (invoice.sapo.vn)"
    mst_tcgp = ("0103243195",)
    hosts = ("sapo.vn",)

    def fetch_pdf(self, invoice: dict) -> bytes:
        codes = lookup_codes(invoice)
        if not codes:
            raise PdfFetchError(f"Không có mã tra cứu ({self.name})", "no_code")
        s = new_session()
        last = ""
        for code in codes[:3]:
            r = request(s, "POST", API, json={"invoice": {"reference_code": code}},
                        headers={"Origin": "https://www.sapo.vn", "Referer": PAGE})
            try:
                js = r.json() or {}
            except ValueError:
                js = {}
            if r.status_code == 404 or (r.status_code == 200 and js.get("errors")):
                err = (js.get("errors") or [{}])[0] or {}
                last = str(err.get("message") or ",".join(err.get("fields") or []) or err.get("code") or "")
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"{self.name} trả HTTP {r.status_code}", "other")
            b64 = ((js.get("invoice") or {}).get("base64") or "").strip()
            if not b64:
                raise PdfFetchError(f"{self.name}: kết quả tra cứu không có PDF", "no_pdf")
            try:
                pdf = base64.b64decode(b64)
            except (binascii.Error, ValueError) as e:
                raise PdfFetchError(f"{self.name}: PDF base64 hỏng", "bad_content") from e
            if not is_pdf(pdf):
                raise PdfFetchError(f"{self.name} trả nội dung không phải PDF", "bad_content")
            check_pdf_is_invoice(pdf, invoice)
            return pdf
        raise PdfFetchError(f"{self.name} không tìm thấy HĐ ({', '.join(codes[:3])}): {last}", "no_pdf")

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Chỉ link tra cứu hóa đơn (`tra-cuu-sapo-invoice` / `invoice.sapo.vn`) có mã; không mở link."""
        if "invoice" not in url.lower():
            return None
        p = link_params(url)
        qr = next((v for k, v in p.items() if k.startswith("qrcode")), None)
        secret = (qr or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


register(SapoAdapter())
