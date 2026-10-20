# pdf_providers/viettel.py
"""
Viettel S-Invoice (vinvoice) — MSTTCGP 0100109106.
Tra cứu: https://vinvoice.viettel.vn/utilities/invoice-search (MST bên bán + mã tra cứu + captcha)

Mã tra cứu (reservationCode): TTKhac "Mã số bí mật" (15 ký tự, vd UY0U5AFNXKNIXN3).
TTKhac "Mã số" (UUID / mã nội bộ) KHÔNG dùng được để tra.

Captcha của trang là slider ghép mảnh (app-slide-captcha: /einvoiceuaa/api/captcha/generate
→ ảnh nền + mảnh ghép, /captcha/verify {token, offsetX} → token gửi kèm ở trường
"recaptcha") — không đọc được bằng model chữ. Nhưng server KHÔNG kiểm captcha (khảo sát
10/2026 trên HĐ thật C26TSA-34983/35464, C26TYY-1104): recaptcha rỗng, sai, hay bỏ hẳn
đều trả PDF. → không giải captcha. Nếu Viettel bật kiểm ở server → lỗi 'captcha' (xem _get).

    POST /api/services/einvoicequery/sync/utility/downloadPDF?taxCode=<MST bán>
         JSON {supplierTaxCode, reservationCode, recaptcha}
      → 200 application/octet-stream + PDF (server dựng)                     : thành công
      → 400 {"message":"INVOICE_NOT_FOUND","data":"Không tìm thấy hóa đơn"}   : sai mã / sai MST
    Dự phòng y như trang (HĐ trên hệ thống cũ "V1"):
    POST /api/services/einvoiceadminapplication/no-auth/invoice-v1/downloadPDF  (cùng tham số)
      → 400 "Không tìm thấy mã số thuế" khi người bán không có trên V1

Đừng dùng GET .../utility/generatePDF (nút "In"): chỉ trả PDF đã dựng sẵn từ lần
downloadPDF trước, HĐ chưa ai xem → 200 body rỗng.
Mọi người bán dùng chung cổng này (không có cổng riêng).
"""
from __future__ import annotations

import unicodedata

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://vinvoice.viettel.vn"
PAGE = f"{BASE}/utilities/invoice-search"
ENDPOINTS = (
    f"{BASE}/api/services/einvoicequery/sync/utility/downloadPDF",                   # hệ thống hiện tại
    f"{BASE}/api/services/einvoiceadminapplication/no-auth/invoice-v1/downloadPDF",  # hệ thống cũ (V1)
)
# Tên trường trong TTKhac chứa mã tra cứu (so khớp sau khi chuẩn hóa NFC + chữ thường)
CODE_FIELDS = ("mã số bí mật", "mã bí mật", "mã tra cứu", "reservationcode")
_NOT_FOUND = ("not_found", "không tìm thấy")


def _norm(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip().lower()


class _NotFound(Exception):
    pass


class ViettelAdapter(PdfAdapter):
    source_id = "viettel"
    name = "Viettel S-Invoice"
    mst_tcgp = ("0100109106",)
    hosts = ("vinvoice.viettel.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail Viettel: vinvoice.viettel.vn/tracuu?tax-code=<MST bán>&secret-code=<mã bí mật>."""
        p = link_params(url)
        mst, secret = p.get("tax-code"), p.get("secret-code")
        if not (mst and secret):
            return None
        return {"nbmst": mst, "ma_tra_cuu": secret, "tt_khac": {"Mã số bí mật": secret}}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = {_norm(k): v for k, v in (invoice.get("tt_khac") or {}).items()}
        for key in CODE_FIELDS:
            if str(tt_khac.get(key) or "").strip():
                return str(tt_khac[key]).strip()
        return str(invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'Mã số bí mật' (mã tra cứu Viettel)", "no_code")
        mst = str(invoice.get("nbmst") or "").strip()
        if not mst:
            raise PdfFetchError("Thiếu MST người bán — Viettel tra theo MST + mã bí mật", "no_code")
        s = new_session()
        msgs = []
        for url in ENDPOINTS:
            try:
                pdf = self._post(s, url, mst, code)
            except _NotFound as e:
                msgs.append(str(e))
                continue
            check_pdf_is_invoice(pdf, invoice)
            return pdf
        raise PdfFetchError(f"Viettel không tìm thấy HĐ (MST {mst}, mã {code}): {'; '.join(msgs)}",
                            "no_pdf")

    @staticmethod
    def _post(s, url: str, mst: str, code: str) -> bytes:
        r = request(s, "POST", url, params={"taxCode": mst},
                    json={"supplierTaxCode": mst, "reservationCode": code, "recaptcha": ""},
                    headers={"Referer": PAGE, "Origin": BASE,
                             "Accept": "application/json, text/plain, */*"})
        if r.status_code == 200:
            if not r.content:
                raise PdfFetchError("Viettel trả body rỗng (PDF chưa sẵn sàng)", "no_pdf")
            if not is_pdf(r.content):
                raise PdfFetchError(
                    f"Viettel trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                    "bad_content")
            return r.content
        try:
            js = r.json()
        except ValueError:
            js = {}
        msg = f"{js.get('message') or ''} {js.get('data') or ''}".strip() if isinstance(js, dict) else ""
        if "captcha" in _norm(msg) or "xác thực" in _norm(msg):
            # Hiện server không kiểm captcha; gặp lỗi này nghĩa là Viettel đã bật → phải giải slider
            raise PdfFetchError(f"Viettel đòi captcha (server mới bật kiểm?): {msg}", "captcha")
        if r.status_code in (400, 404) and any(k in _norm(msg) for k in _NOT_FOUND):
            raise _NotFound(msg)
        raise PdfFetchError(f"Viettel trả HTTP {r.status_code}: {msg or r.content[:200]!r}", "other")


register(ViettelAdapter())
