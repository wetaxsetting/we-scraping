"""
FAST (Công ty Cổ phần phần mềm QLDN Fast) — MSTTCGP 0100727825.
Tra cứu: https://einvoice.fast.com.vn/  (cổng thật; invoice.fast.com.vn/tra-cuu-hoa-don-dien-tu
là trang WordPress chỉ dẫn, không tra bằng KeySearch được — xem docs/Danh_sach_NCC.xlsx).

Nền ASP.NET WebForms. Trang có ô "Mã nhận hóa đơn" (KeySearch) + captcha ảnh 100x30 (5 chữ
hoa/thường) ở ô "Mã xác thực". Mọi gọi đi qua 1 handler, phân biệt bằng tham số t:
    POST /AppHandler/EInvoiceQuery.ashx?p=<base64(KeySearch)>&t=2&c=<captcha>  → tra cứu (xem HĐ)
         {"error":"1","invoice":"<số HĐ>",...}  thấy · "error":"0"  sai captcha / không thấy
    GET  /AppHandler/EInvoiceQuery.ashx?p=<base64(KeySearch)>&t=4&c=<captcha>&n=<số HĐ> → PDF
    GET  ...&t=6...  → XML gốc (có chữ ký)

Captcha KHÔNG bị kiểm ở bước tải (t=4): gửi captcha RỖNG (c="") vẫn trả đúng PDF — đã đo
23/10/2026 trên 6 HĐ thật (C23TQA-473, C23TSH-13, C25TVC-30, C26TVC-2899, K23TDP-238/842):
6/6 tải được PDF có đúng số HĐ, không cần giải captcha (captcha chỉ chặn bước t=2 "xem").
Nên adapter đi thẳng t=4 với captcha rỗng — không cần nhóm model nào.

Mã nhận HĐ = KeySearch trong TTKhac của XML (parser lưu ở tt_khac["KeySearch"], vd
"AQAAAB5Cs4dprpprtUb4...=007216"). `n` (số HĐ) = SHDon. `p` = base64 chuẩn của KeySearch
(đúng hàm U$ của trang). First Trust (0104493085) dùng chung cổng này.
"""
from __future__ import annotations

import base64
import re
import time

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

BASE = "https://einvoice.fast.com.vn"
HANDLER = f"{BASE}/AppHandler/EInvoiceQuery.ashx"
# "... hóa đơn điện tử số 5470" / "(... E-Invoices No. 297)"
_SUBJECT_NO_RE = re.compile(r"(?i)(?:h[oó]á? đơn[^\n]{0,30}?\bsố|\bE-?Invoices? No\.)\s*:?\s*0*(\d{1,8})\b")


class FastAdapter(PdfAdapter):
    source_id = "fast"
    name = "FAST e-Invoice"
    mst_tcgp = ("0100727825",)
    hosts = ("einvoice.fast.com.vn",)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        """KeySearch (mã nhận HĐ) — ưu tiên TTKhac, rồi ma_tra_cuu."""
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("KeySearch") or tt_khac.get("KeySeatch")
                   or invoice.get("ma_tra_cuu") or "").strip()

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link mail FAST: einvoice.fast.com.vn/?c=<KeySearch thô>. Chỉ đọc tham số, không mở link.
        Số HĐ (bắt buộc cho t=4) lấy từ tiêu đề mail: "Thông báo phát hành hóa đơn điện tử số 297".
        Đọc nhầm số thì check_pdf_is_invoice chặn, không ghi nhầm PDF."""
        from .common import link_params
        key = (link_params(url).get("c") or (code or "")).strip().replace(" ", "+")
        if not key:
            return None
        inv = {"ma_tra_cuu": key, "tt_khac": {"KeySearch": key}}
        m = _SUBJECT_NO_RE.search(mail_text or "")
        if m:
            inv["shdon"] = m.group(1)
        return inv

    def fetch_pdf(self, invoice: dict) -> bytes:
        key = self.lookup_code(invoice)
        if not key:
            raise PdfFetchError("XML không có 'KeySearch' (mã nhận hóa đơn FAST)", "no_code")
        shdon = str(invoice.get("shdon") or "").strip()
        if not shdon:
            raise PdfFetchError("Thiếu số hóa đơn (SHDon) để tải PDF FAST", "no_code")
        p = base64.b64encode(key.encode("utf-8")).decode("ascii")
        s = new_session()
        request(s, "GET", f"{BASE}/")                     # lấy cookie phiên
        r = request(s, "GET", HANDLER, headers={"Referer": f"{BASE}/"},
                    params={"p": p, "t": "4", "c": "", "n": shdon,
                            "r": str(int(time.time() * 1000))})
        if r.status_code != 200:
            raise PdfFetchError(f"FAST trả HTTP {r.status_code}", "other")
        if not is_pdf(r.content):
            # Handler trả thân rỗng khi mã sai / HĐ không tồn tại (Content-Type không phải pdf).
            raise PdfFetchError(
                f"FAST không trả PDF cho mã {key[:12]}… số {shdon} "
                f"(Content-Type {r.headers.get('Content-Type') or 'rỗng'}, {len(r.content)} byte)",
                "no_pdf")
        check_pdf_is_invoice(r.content, invoice)
        return r.content


register(FastAdapter())
