"""
VietInfo (CTCP Công nghệ VietInfo) — MSTTCGP 0311928954.
Tra cứu: https://tracuuhoadon.vietinfo.tech/  ("Mã tra cứu" + "Ngày hóa đơn" dd/MM/yyyy, không captcha).

ASP.NET MVC, khảo sát 27/10/2026 (đọc HTML/JS + gửi mã bịa):

    POST /   form MaTraCuu, NgayHoaDon=dd/MM/yyyy, btnSearch
      → 200 HTML, bảng kết quả (tbody) có các ô ẩn #inputFileHoaDon (đường dẫn file .xml của HĐ),
        #inputHoaDonId, #inputSoHoaDon                         : thấy HĐ
      → 200 HTML, tbody rỗng                                     : không thấy → 'no_pdf'
    GET  /Home/TaiFileHoaDon?FileHoaDon=<đường dẫn>   → chuỗi base64 của file
        (JS "Bản thể hiện" đổi đuôi .xml → .pdf; "Tải file" giữ .xml)

Mã: TTKhac "Mã tra cứu" / ma_tra_cuu; ngày lập lấy từ XML. Không có ngày → 'no_code'.
Chưa có HĐ thật (DB 27/10: 0 HĐ MSTTCGP 0311928954, 0 link mail) — chỉ kiểm phản hồi "không tìm thấy";
nhánh thành công dựng theo JS của trang, cần kiểm lại khi có HĐ thật.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import html as _html
import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request
from .formlookup import lookup_code

BASE = "https://tracuuhoadon.vietinfo.tech"
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def date_vn(value) -> str | None:
    """ngay_lap (date / 'YYYY-MM-DD…' / 'dd/mm/yyyy') → 'dd/MM/yyyy'."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%d/%m/%Y")
    s = str(value or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    m = _DATE_RE.match(s)
    if m:
        return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"
    return None


def result_files(page: str) -> list:
    """Giá trị các ô ẩn id="inputFileHoaDon" trong bảng kết quả (thứ tự thuộc tính tùy ý)."""
    out = []
    for tag in _INPUT_RE.findall(page or ""):
        if re.search(r'\bid\s*=\s*["\']inputFileHoaDon["\']', tag, re.I):
            m = re.search(r'\bvalue\s*=\s*["\']([^"\']*)["\']', tag, re.I)
            if m and m.group(1).strip():
                out.append(_html.unescape(m.group(1).strip()))
    return out


class VietInfoAdapter(PdfAdapter):
    source_id = "vietinfo"
    name = "VietInfo (tracuuhoadon.vietinfo.tech)"
    mst_tcgp = ("0311928954",)
    hosts = ("tracuuhoadon.vietinfo.tech",)

    def _search(self, invoice: dict):
        code = lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có mã tra cứu (VietInfo)", "no_code")
        day = date_vn(invoice.get("ngay_lap"))
        if not day:
            raise PdfFetchError("Thiếu ngày lập HĐ — VietInfo tra bằng mã + ngày", "no_code")
        s = new_session()
        r = request(s, "POST", f"{BASE}/", data={"MaTraCuu": code, "NgayHoaDon": day, "btnSearch": ""},
                    headers={"Referer": f"{BASE}/"})
        if r.status_code != 200:
            raise PdfFetchError(f"VietInfo tra cứu trả HTTP {r.status_code}", "other")
        files = result_files(r.text)
        if not files:
            raise PdfFetchError(f"VietInfo không tìm thấy HĐ (mã {code}, ngày {day})", "no_pdf")
        return s, files[0]

    @staticmethod
    def _download(s, path: str) -> bytes:
        r = request(s, "GET", f"{BASE}/Home/TaiFileHoaDon", params={"FileHoaDon": path},
                    headers={"Referer": f"{BASE}/", "X-Requested-With": "XMLHttpRequest"})
        if r.status_code != 200:
            raise PdfFetchError(f"VietInfo TaiFileHoaDon trả HTTP {r.status_code}", "other")
        body = (r.text or "").strip().strip('"')
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as e:
            raise PdfFetchError("VietInfo TaiFileHoaDon không trả base64", "bad_content") from e

    def fetch_pdf(self, invoice: dict) -> bytes:
        s, path = self._search(invoice)
        pdf = self._download(s, re.sub(r"\.xml$", ".pdf", path, flags=re.I))
        if not is_pdf(pdf):
            raise PdfFetchError("VietInfo trả nội dung không phải PDF", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        s, path = self._search(invoice)
        if not path.lower().endswith(".xml"):
            raise PdfFetchError(f"VietInfo: file kết quả không phải .xml ({path})", "bad_content")
        xml = self._download(s, path)
        head = xml.lstrip(b"\xef\xbb\xbf \r\n\t")[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError("VietInfo trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Cần mã + ngày HĐ: chỉ dùng khi mail có mã và đúng 1 ngày dd/mm/yyyy. Không mở link."""
        secret = (code or "").strip()
        days = {f"{int(d):02d}/{int(m):02d}/{y}" for d, m, y in _DATE_RE.findall(mail_text or "")}
        if not secret or len(days) != 1:
            return None
        return {"ma_tra_cuu": secret, "ngay_lap": days.pop(), "tt_khac": {"Mã tra cứu": secret}}


register(VietInfoAdapter())
