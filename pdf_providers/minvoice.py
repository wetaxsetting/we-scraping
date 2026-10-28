# pdf_providers/minvoice.py
"""
M-invoice — MSTTCGP 0106026495 (cả chi nhánh 0106026495-001, registry tự quy về công ty mẹ).
Tra cứu: https://tracuuhoadon.minvoice.com.vn/tra-cuu-hoa-don

Mỗi người bán có subdomain riêng (<mst>.minvoice.com.vn / .minvoice.app) nhưng KHÔNG cần:
cổng tra cứu chung tra được HĐ của mọi người bán theo MST + số bảo mật. Không captcha
(form trên trang không có), không cookie. Khảo sát 10/2026 trên HĐ thật C26TCA-11001
(0104957749-001) và C26MBH-2703 (máy tính tiền, 0110097153):

    GET /api/Search/SearchInvoice?masothue=<MST người bán>&sobaomat=<mã>&type=PDF&inchuyendoi=false
      → 200 application/pdf                          : thành công (PDF server dựng sẵn, có chữ)
      → 500 JSON {"code":"5000","message":"Thông tin tra cứu không chính xác..."}
                                                       : sai MST/mã → 'no_pdf' (KHÔNG phải lỗi server)
      → 400 JSON (thiếu masothue)

MST người bán giữ hay bỏ dấu '-' đều được. Mã phân biệt hoa thường (viết thường → 5000).
Mã: TTKhac "Số bảo mật" (HĐ thường) hoặc "Mã tra cứu" (HĐ máy tính tiền), cả hai 16 ký tự hex.

Mail "THÔNG BÁO XUẤT HÓA ĐƠN SỐ 2848" (27/10, user kiểm tay): link chỉ là trang chủ
tracuuhoadon.minvoice.vn, nội dung ghi "MST người bán: 0313217924" + "Số bảo mật: E864CA9903996CF4"
→ đủ để gọi API trên (mail_reader lưu MST người bán, process_links truyền vào mail_text). Thật 2/2
(Quán Nem 1C24TYY-2848, -3581).
"""
from __future__ import annotations

import re

import requests

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import TIMEOUT, check_pdf_is_invoice, is_pdf, new_session

BASE = "https://tracuuhoadon.minvoice.com.vn"
API = f"{BASE}/api/Search/SearchInvoice"
CODE_KEYS = ("Số bảo mật", "Mã tra cứu", "Mã bảo mật", "sobaomat")
_CODE_RE = re.compile(r"^[0-9A-F]{16}$")
_MAIL_MST_RE = re.compile(r"^MST người bán:\s*(\d{10}(?:-\d{3})?)$", re.M)
_SUBJECT_NO_RE = re.compile(r"H[ÓO]A\s*[ĐD][ƠO]N\s*S[ỐO]\s*0*(\d+)", re.I)


class MinvoiceAdapter(PdfAdapter):
    source_id = "minvoice"
    name = "M-invoice"
    mst_tcgp = ("0106026495",)
    hosts = ("tracuuhoadon.minvoice.vn", "tracuuhoadon.minvoice.com.vn")

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link trang chủ + "MST người bán" + "Số bảo mật" trong mail (không mở link). Thiếu 1 trong 2 → None."""
        lines = (mail_text or "").splitlines()
        codes = [c for c in dict.fromkeys([code or ""] + [ln.strip() for ln in lines[1:]]) if _CODE_RE.match(c)]
        msts = _MAIL_MST_RE.findall(mail_text or "")
        if len(codes) != 1 or len(set(msts)) != 1:
            return None
        inv = {"nbmst": msts[0], "ma_tra_cuu": codes[0], "tt_khac": {"Số bảo mật": codes[0]}}
        m = _SUBJECT_NO_RE.search(lines[0] if lines else "")
        if m:
            inv["shdon"] = int(m.group(1))
        return inv

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for key in CODE_KEYS:
            if str(tt_khac.get(key) or "").strip():
                return str(tt_khac[key]).strip()
        return (invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'Số bảo mật' / 'Mã tra cứu' (M-invoice)", "no_code")
        mst = (invoice.get("nbmst") or "").strip()
        if not mst:
            raise PdfFetchError("Thiếu MST người bán — M-invoice cần MST + số bảo mật", "no_code")
        s = new_session()
        r = self._get(s, {"masothue": mst, "sobaomat": code, "type": "PDF", "inchuyendoi": "false"})
        pdf = r.content
        if not is_pdf(pdf):
            raise PdfFetchError(
                f"M-invoice trả nội dung không phải PDF ({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    @staticmethod
    def _get(s, params: dict):
        """Như common.request() nhưng đọc JSON lỗi trước: 'không tìm thấy' của M-invoice là HTTP 500."""
        try:
            r = s.request("GET", API, params=params, timeout=TIMEOUT,
                          headers={"Accept": "application/json, text/plain, */*",
                                   "Referer": f"{BASE}/tra-cuu-hoa-don"})
        except requests.Timeout as e:
            raise PdfFetchError(f"Timeout {API}: {e}", "network") from e
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối {API}: {e}", "network") from e
        if r.status_code == 200:
            return r
        if r.status_code == 429:
            raise PdfFetchError(f"429 từ {API}", "rate_limit")
        try:
            err = r.json()
        except ValueError:
            err = None
        msg = str((err or {}).get("message") or (err or {}).get("title") or "").strip()
        if isinstance(err, dict) and (str(err.get("code")) == "5000" or "không chính xác" in msg.lower()):
            raise PdfFetchError(f"M-invoice không tìm thấy HĐ (MST {params['masothue']}, "
                                f"mã {params['sobaomat']}): {msg}", "no_pdf")
        if r.status_code >= 500:
            raise PdfFetchError(f"HTTP {r.status_code} từ M-invoice: {msg or r.text[:200]}", "server")
        raise PdfFetchError(f"M-invoice trả HTTP {r.status_code}: {msg or r.text[:200]}", "other")


register(MinvoiceAdapter())
