# pdf_providers/atis.py
"""
ATIS — MSTTCGP 0107732197. Tra cứu: https://tracuuhoadon.atis.com.vn/ (HĐ vé trạm BOT, bảo hiểm TIC, DANA...).

Cùng phần mềm với cổng VETC (xem vetc.py — ZIP của VETC cũng tên ATIS_<số>.xml): trang Spring/Thymeleaf,
không captcha, không cần MST người bán:
    POST /   searchType=SECURE_ID, secureId=<Mã tra cứu> (9-13 ký tự, phân biệt hoa thường)
        → thấy HĐ: link /download/<mã> (theo VETC)
        → không thấy: div#secureIdError2 "Không tìm thấy hóa đơn, xin vui lòng kiểm tra lại mã tra cứu của bạn!"
    GET  /download/<mã>   → ZIP {ATIS_<số>.xml, .html, .pdf} (theo VETC)
(Tra theo "Mã vé" + trạm (sourceOrg) cũng có, adapter không dùng.)

vetc.py để BASE ở mức module nên không kế thừa được → chép luồng, đổi host.

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy (đúng thông báo + id như VETC).
"""
from __future__ import annotations

import io
import re
import zipfile

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

_NOT_FOUND = "không tìm thấy hóa đơn"
_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,16}$")          # phân biệt hoa thường → không đổi case


class AtisAdapter(PdfAdapter):
    source_id = "atis"
    name = "ATIS (tracuuhoadon.atis.com.vn)"
    mst_tcgp = ("0107732197",)
    hosts = ("tracuuhoadon.atis.com.vn",)
    BASE = "https://tracuuhoadon.atis.com.vn"

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for c in (tt_khac.get("Mã tra cứu"), tt_khac.get("Mã tra cứu hóa đơn"), invoice.get("ma_tra_cuu")):
            c = str(c or "").strip()
            if _CODE_RE.match(c):
                return c
        return ""

    def _zip(self, invoice: dict) -> zipfile.ZipFile:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'Mã tra cứu' (ATIS)", "no_code")
        base = self.BASE
        s = new_session()
        request(s, "GET", f"{base}/")
        r = request(s, "POST", f"{base}/",
                    data={"searchType": "SECURE_ID", "secureId": code,
                          "sourceOrg": "", "ticketCode": "", "plate": ""},
                    headers={"Referer": f"{base}/", "Origin": base})
        if r.status_code != 200:
            raise PdfFetchError(f"Tra cứu ATIS trả HTTP {r.status_code}", "other")
        if f'href="/download/{code}"' not in r.text:
            if _NOT_FOUND in r.text.lower() or 'id="secureIdError2"' in r.text:
                raise PdfFetchError(f"ATIS không tìm thấy HĐ (mã {code})", "no_pdf")
            raise PdfFetchError("Trang kết quả ATIS không đúng dạng (thiếu link tải)", "bad_content")
        z = request(s, "GET", f"{base}/download/{code}", headers={"Referer": f"{base}/"})
        if z.status_code != 200:
            raise PdfFetchError(f"Tải ZIP ATIS trả HTTP {z.status_code}", "other")
        try:
            return zipfile.ZipFile(io.BytesIO(z.content))
        except zipfile.BadZipFile as e:
            raise PdfFetchError(f"ATIS trả không phải ZIP: {e}", "bad_content") from e

    @staticmethod
    def _member(zf: zipfile.ZipFile, ext: str) -> bytes:
        name = next((n for n in zf.namelist() if n.lower().endswith(ext)), None)
        if name is None:
            raise PdfFetchError(f"ZIP của ATIS không có file {ext}", "no_pdf")
        return zf.read(name)

    def fetch_pdf(self, invoice: dict) -> bytes:
        pdf = self._member(self._zip(invoice), ".pdf")
        if not is_pdf(pdf):
            raise PdfFetchError("File PDF trong ZIP của ATIS không hợp lệ", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        xml = self._member(self._zip(invoice), ".xml")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if not m:
            raise PdfFetchError("XML ATIS không có SHDon", "bad_content")
        if shdon and int(m.group(1)) != int(shdon):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link trang chủ + mã tra cứu trong mail. Không mở link."""
        code = (code or "").strip()
        if not _CODE_RE.match(code):
            return None
        return {"ma_tra_cuu": code, "tt_khac": {"Mã tra cứu": code}}


register(AtisAdapter())
