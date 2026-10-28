"""
EPAY (tracuuhoadon.epayjsc.com) — MSTTCGP 0108269133.

Cổng là bản white-label của phần mềm tra cứu M-invoice (cùng API với minvoice.py; trang lỗi của
1invoice.vn còn lộ đường dẫn "D:\\Minvoice\\Project\\2025\\NCCA\\SearchInvoice"). SPA gọi API
cùng host, không captcha, không cookie, không reCAPTCHA (khảo sát 27/10/2026, đọc bundle
/assets/index-*.js — trang tra cứu ở chunk index-DBd5Rb6u.js):

    GET /api/Search/SearchInvoice?masothue=<MST người bán>&sobaomat=<mã>&type=PDF&inchuyendoi=false
        → PDF (type=HTML: bản xem trước trên trang)
    GET /api/Search/DownloadXml?masothue=<MST người bán>&sobaomat=<mã>   → XML
Không tìm thấy (đã thử mã bịa):
    EPAY:  400 {"code":1007,"message":"Không tìm thấy hóa đơn/biên lai hợp lệ với thông tin đầu vào..."}
    ATOM:  500 {"code":"5000","message":"Get Tenant fail because not found Tenant ..."} (MST lạ)
           / {"code":"5000","message":"Có lỗi xảy ra"} — như M-invoice: 5000 = không tìm thấy.
Link mail / URL trang: ?taxCode=<MST bán>&checkCode=<mã>&tab=invoice (trang tự điền form).

Mã: "Số bảo mật"/"Mã tra cứu" như M-invoice (tên trường TTKhac chưa xác nhận — chưa có HĐ thật).
Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy.
"""
from __future__ import annotations

import re

import requests

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import TIMEOUT, check_pdf_is_invoice, is_pdf, link_params, new_session
from .lookup_codes import lookup_codes

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
CODE_KEYS = ("Số bảo mật", "Mã tra cứu", "Mã bảo mật", "Mã tra cứu hóa đơn", "sobaomat")
NOT_FOUND_CODES = {"1007", "5000"}


class EpayAdapter(PdfAdapter):
    """Cổng tra cứu M-invoice white-label: đổi BASE + mst_tcgp + hosts ở lớp con."""
    source_id = "epay"
    name = "EPAY (tracuuhoadon.epayjsc.com)"
    mst_tcgp = ("0108269133",)
    hosts = ("tracuuhoadon.epayjsc.com",)
    BASE = "https://tracuuhoadon.epayjsc.com"

    @staticmethod
    def codes(invoice: dict) -> list:
        return lookup_codes(invoice, CODE_KEYS, _CODE_RE)

    def _get(self, invoice: dict, path: str, extra: dict):
        codes = self.codes(invoice)
        if not codes:
            raise PdfFetchError(f"XML không có số bảo mật / mã tra cứu ({self.name})", "no_code")
        mst = (invoice.get("nbmst") or "").strip()
        if not mst:
            raise PdfFetchError(f"Thiếu MST người bán — {self.name} cần MST + mã", "no_code")
        s = new_session()
        for code in codes:
            r = self._call(s, f"{self.BASE}/api/Search/{path}", {"masothue": mst, "sobaomat": code, **extra})
            if r is not None:
                return r
        raise PdfFetchError(f"{self.name} không tìm thấy HĐ (MST {mst}, mã {', '.join(codes)})", "no_pdf")

    def _call(self, s, url: str, params: dict):
        """Response 200, hoặc None khi 'không tìm thấy'. Lỗi khác → PdfFetchError."""
        try:
            r = s.request("GET", url, params=params, timeout=TIMEOUT,
                          headers={"Accept": "application/json, text/plain, */*", "Referer": f"{self.BASE}/"})
        except requests.Timeout as e:
            raise PdfFetchError(f"Timeout {url}: {e}", "network") from e
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối {url}: {e}", "network") from e
        if r.status_code == 200:
            return r
        if r.status_code == 429:
            raise PdfFetchError(f"429 từ {url}", "rate_limit")
        try:
            err = r.json()
        except ValueError:
            err = None
        err = err if isinstance(err, dict) else {}
        msg = str(err.get("message") or "").strip()
        low = msg.lower()
        if str(err.get("code")) in NOT_FOUND_CODES or "không tìm thấy" in low or "không chính xác" in low:
            return None
        if r.status_code >= 500:
            raise PdfFetchError(f"HTTP {r.status_code} từ {self.name}: {msg or r.text[:200]}", "server")
        raise PdfFetchError(f"{self.name} trả HTTP {r.status_code}: {msg or r.text[:200]}", "other")

    def fetch_pdf(self, invoice: dict) -> bytes:
        r = self._get(invoice, "SearchInvoice", {"type": "PDF", "inchuyendoi": "false"})
        if not is_pdf(r.content):
            raise PdfFetchError(f"{self.name} trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        r = self._get(invoice, "DownloadXml", {})
        xml = r.content or b""
        if b"<HDon" not in xml[:5000]:
            raise PdfFetchError(f"{self.name} trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link ?taxCode=<MST bán>&checkCode=<mã>; thiếu MST thì không dùng được. Không mở link."""
        p = link_params(url)
        secret = (p.get("checkcode") or p.get("sobaomat") or code or "").strip()
        mst = (p.get("taxcode") or p.get("masothue") or "").strip()
        if not _CODE_RE.match(secret) or not re.fullmatch(r"\d{10}(-\d{3})?|\d{12}", mst):
            return None
        return {"nbmst": mst, "ma_tra_cuu": secret, "tt_khac": {"Số bảo mật": secret}}


register(EpayAdapter())
