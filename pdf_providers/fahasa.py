"""
Fahasa (Công ty CP Phát hành sách TP.HCM, MST người bán 0304132047) — cổng riêng hoadon.fahasa.com.
Phần mềm là bản tự host của FPT eInvoice (XML ghi MSTTCGP 0104128565 = FPT, ent "fhs"), nhưng
KHÁC cổng chung tracuuhoadon.fpt.com.vn (cổng đó server kiểm reCAPTCHA v3 thật, xem CLAUDE.md 2.6):
trang search.html chỉ nạp reCAPTCHA khi captchaType == 2 mà bản Fahasa không khai biến này
(codebase/config.js không có) → không widget nào chạy, và API trả HĐ khi gửi token RỖNG.
Đã kiểm 26/10/2026: 5/5 mã thật gửi token rỗng đều ra PDF; mã bịa → HTTP 500
"Không tìm thấy hóa đơn mã tra cứu: ...". Adapter KHÔNG gửi/giải token nào.

Vì MSTTCGP là của FPT (cổng chung đang bị reCAPTCHA chặn) nên adapter KHÔNG nhận mst_tcgp —
nhận HĐ theo host link mail (hoadon.fahasa.com) hoặc set-provider cho người bán Fahasa.

Mã tra cứu = "sec" (10 ký tự, vd 25C51N63AL) — chỉ có trong mail Fahasa gửi
(link http://hoadon.fahasa.com/search.html?sec=<mã>&action=search, kèm "Mã tra cứu: <mã>");
XML Fahasa không có TTKhac nào chứa mã này.
  GET /api/seek/search1?sec=<mã>&action=search&token=  → {"pdf": "data:application/pdf;base64, ...", status, ...}
  GET /api/seek/xml1?sec=<mã>&action=search&token=     → {"xml": "<HDon>...có chữ ký..."}
Tiêu đề mail có số HĐ: "Hóa đơn điện tử số 00019354 – CÔNG TY CỔ PHẦN PHÁT HÀNH SÁCH TPHCM – FAHASA ...".
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from urllib.parse import urlsplit

import requests

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://hoadon.fahasa.com"
SELLER_MST = "0304132047"
_SEC_RE = re.compile(r"^[A-Za-z0-9]{6,20}$")
_SUBJECT_NO_RE = re.compile(r"(?i)h[oó][aá]?\s*đơn[^\n]{0,30}?\bsố\s*:?\s*0*(\d{1,10})\b")


class FahasaAdapter(PdfAdapter):
    source_id = "fahasa"
    name = "Fahasa (hoadon.fahasa.com, FPT tự host)"
    mst_tcgp = ()                           # 0104128565 là FPT: cổng chung bị reCAPTCHA, không nhận
    hosts = ("hoadon.fahasa.com",)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("sec") or tt_khac.get("Mã tra cứu") or invoice.get("ma_tra_cuu") or "").strip()

    def _get(self, path: str, sec: str) -> dict:
        s = new_session()
        request(s, "GET", f"{BASE}/search.html")
        try:
            r = s.get(f"{BASE}/api/seek/{path}", params={"sec": sec, "action": "search", "token": ""},
                      headers={"Referer": f"{BASE}/search.html"}, timeout=60)
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối Fahasa: {e}", "network") from e
        if r.status_code == 500 and "không tìm thấy" in (r.text or "").lower():
            raise PdfFetchError(f"Fahasa không tìm thấy HĐ mã {sec}", "no_pdf")
        if r.status_code >= 500:
            raise PdfFetchError(f"Fahasa trả HTTP {r.status_code}", "server")
        try:
            return r.json()
        except ValueError as e:
            raise PdfFetchError(f"Fahasa trả HTTP {r.status_code}, không phải JSON", "bad_content") from e

    def _sec(self, invoice: dict) -> str:
        sec = self.lookup_code(invoice)
        if not sec:
            raise PdfFetchError("Không có mã tra cứu Fahasa (chỉ có trong mail NCC gửi)", "no_code")
        if not _SEC_RE.match(sec):
            raise PdfFetchError(f"Mã tra cứu Fahasa không đúng dạng: {sec!r}", "no_code")
        return sec

    def fetch_pdf(self, invoice: dict) -> bytes:
        j = self._get("search1", self._sec(invoice))
        b64 = str(j.get("pdf") or "").split(",", 1)[-1].strip()
        try:
            pdf = base64.b64decode(b64)
        except (binascii.Error, ValueError) as e:
            raise PdfFetchError("Trường pdf của Fahasa không phải base64", "bad_content") from e
        if not is_pdf(pdf):
            raise PdfFetchError("Fahasa không trả PDF", "no_pdf")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        xml = str(self._get("xml1", self._sec(invoice)).get("xml") or "").encode("utf-8")
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if not m:
            raise PdfFetchError("Fahasa không trả XML HĐ", "no_pdf")
        shdon = str(invoice.get("shdon") or "").strip()
        if shdon and int(m.group(1)) != int(shdon):
            raise PdfFetchError(f"XML Fahasa tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link mail: search.html?sec=<mã>&action=search; link trang tra cứu trần dùng mã trong mail.
        Số HĐ lấy từ tiêu đề mail (đọc nhầm thì check_pdf_is_invoice chặn). Không mở link."""
        host = (urlsplit(url or "").hostname or "").lower()
        if host not in self.hosts:
            return None
        sec = (link_params(url).get("sec") or code or "").strip()
        if not _SEC_RE.match(sec):
            return None
        inv = {"ma_tra_cuu": sec, "nbmst": SELLER_MST, "tt_khac": {"sec": sec}}
        m = _SUBJECT_NO_RE.search(unicodedata.normalize("NFC", mail_text or ""))
        if m:
            inv["shdon"] = m.group(1)
        return inv


register(FahasaAdapter())
