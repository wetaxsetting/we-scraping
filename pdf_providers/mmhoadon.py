"""
MOMO E-INVOICE / Dịch vụ Di động Trực tuyến (mmhoadon) — MSTTCGP 0305289153.
Tra cứu: https://tracuu.mmhoadon.vn/  (SPA, 1 ô "mã tra cứu", không captcha, không cookie).

Khảo sát 27/10/2026 (đọc bundle /assets/index-*.js + HoaDonView-*.js). API ở host riêng:
    GET https://api-prod.mmhoadon.vn/api/hoa-don/ma-tra-cuu/<mã>
        → 200 {"status_code":200,"is_success":true,"data":<id HĐ>}   data = 0: KHÔNG tìm thấy
    GET .../api/hoa-don/v2/<id>/pdf?ma_tra_cuu=<mã>&page_size=10        → nút "Tải xuống" (PDF)
    GET .../api/hoa-don/v2/<id>/download?ma_tra_cuu=<mã>                → nút "Tải xuống XML"
    (GET .../v2/<id>/print?ma_tra_cuu=… → HTML xem trước, không dùng)
Trang chống bấm nhanh (2 s) chỉ ở phía trình duyệt. Không có reCAPTCHA.

Mã tra cứu: tên trường TTKhac chưa biết (chưa có HĐ thật) → lấy mọi trường kiểu "mã tra cứu"
(lookup_codes). Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy (data=0).
"""
from __future__ import annotations

import re
from urllib.parse import quote

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .lookup_codes import lookup_codes

SITE = "https://tracuu.mmhoadon.vn"
API = "https://api-prod.mmhoadon.vn/api/hoa-don"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
HEADERS = {"Accept": "application/json, text/plain, */*", "Origin": SITE, "Referer": f"{SITE}/"}


class MmHoaDonAdapter(PdfAdapter):
    source_id = "mmhoadon"
    name = "MOMO E-INVOICE (mmhoadon)"
    mst_tcgp = ("0305289153",)
    hosts = ("tracuu.mmhoadon.vn", "mmhoadon.vn")

    @staticmethod
    def codes(invoice: dict) -> list:
        return lookup_codes(invoice, ("Mã tra cứu", "Mã tra cứu hóa đơn", "MaTraCuu", "MTCuu"), _CODE_RE)

    def _find(self, s, invoice: dict):
        """(mã, id) của HĐ đầu tiên tìm thấy."""
        codes = self.codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu (MOMO E-INVOICE)", "no_code")
        for code in codes:
            r = request(s, "GET", f"{API}/ma-tra-cuu/{quote(code, safe='')}", headers=HEADERS)
            try:
                js = r.json() or {}
            except ValueError:
                raise PdfFetchError(f"MOMO E-INVOICE trả HTTP {r.status_code} không phải JSON", "other")
            if r.status_code != 200 or not js.get("is_success"):
                raise PdfFetchError(f"MOMO E-INVOICE lỗi tra cứu (HTTP {r.status_code}): "
                                    f"{js.get('message') or ''}", "other")
            inv_id = js.get("data")
            if inv_id:
                return code, inv_id
        raise PdfFetchError(f"MOMO E-INVOICE không tìm thấy HĐ ({', '.join(codes)})", "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        s = new_session()
        code, inv_id = self._find(s, invoice)
        r = request(s, "GET", f"{API}/v2/{inv_id}/pdf",
                    params={"ma_tra_cuu": code, "page_size": 10}, headers=HEADERS)
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(f"MOMO E-INVOICE trả nội dung không phải PDF (HTTP {r.status_code}, "
                                f"{r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        s = new_session()
        code, inv_id = self._find(s, invoice)
        r = request(s, "GET", f"{API}/v2/{inv_id}/download", params={"ma_tra_cuu": code}, headers=HEADERS)
        xml = r.content or b""
        if r.status_code != 200 or b"<HDon" not in xml[:5000]:
            raise PdfFetchError(f"MOMO E-INVOICE trả nội dung không phải XML (HTTP {r.status_code})",
                                "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mã lấy từ mail (hoặc tham số ma_tra_cuu/code trên link). Không mở link."""
        p = link_params(url)
        secret = (code or p.get("ma_tra_cuu") or p.get("matracuu") or p.get("code") or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


register(MmHoaDonAdapter())
