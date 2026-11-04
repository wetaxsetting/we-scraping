# pdf_providers/manual_portals.py
"""
NCC có cổng tra cứu dùng reCAPTCHA (server kiểm token thật) — chưa có adapter tự tra.

- FPT (0104128565): đã chuyển sang adapter `fpt_import.py` (job nhập JSON do script trình duyệt tải về).
- HT (0315638251), EFY (0102519041): ĐANG NGHIÊN CỨU (reCAPTCHA v2 / v2 Enterprise). 03/11 lấy được
  HT 2, EFY 6 PDF bằng cách người tích reCAPTCHA, script bắt PDF.

Khai báo ở đây để job / WeInvoice nhận đúng NCC (source_id, tên, cổng). `fetch_pdf` ném `no_portal`:
task nằm ở `no_source/no_portal` (hiện trong `status`, không tự vào lại hộp chờ) cho tới khi có adapter.

Cố ý KHÔNG khai `hosts`: nếu khai, `process_links` gọi `invoice_from_link` → None → link mail
bị đánh `ignored` vĩnh viễn; không khai thì link giữ `new`.
"""
from __future__ import annotations

from . import register
from .base import PdfAdapter, PdfFetchError


class ManualPortalAdapter(PdfAdapter):
    portal: str = ""
    captcha: str = ""
    lookup_key: str = ""      # tra bằng gì
    state: str = ""           # tình trạng hiện tại

    def fetch_pdf(self, invoice: dict) -> bytes:
        raise PdfFetchError(
            f"{self.name}: cổng {self.portal} có {self.captcha} — {self.state}; "
            f"tra bằng {self.lookup_key}", "no_portal")


class HtAdapter(ManualPortalAdapter):
    source_id = "ht"
    name = "HT Invoice"
    mst_tcgp = ("0315638251",)
    portal = "laphoadon.htinvoice.vn"
    captcha = "reCAPTCHA v2"
    lookup_key = "mã tra cứu (TTKhac trong XML)"
    state = "đang nghiên cứu"


class EfyAdapter(ManualPortalAdapter):
    source_id = "efy"
    name = "EFY iHoadon"
    mst_tcgp = ("0102519041",)
    portal = "tracuu.ihoadon.vn"
    captcha = "reCAPTCHA v2 Enterprise"
    lookup_key = "số HĐ + mã `mtc` (chỉ có trong mail)"
    state = "đang nghiên cứu"


for _cls in (HtAdapter, EfyAdapter):
    register(_cls())
