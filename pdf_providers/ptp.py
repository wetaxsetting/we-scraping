"""
PTP-Invoice (CTCP Dịch vụ viễn thông và in — PTP) — MSTTCGP 0100687474.
Tra cứu: https://hoadondientu-ptp.vn/tra-cuu/  (MST người bán + "Số bảo mật", không captcha).

Giao diện dựng lại từ theme của minvoice.vn, ô nhập giống M-Invoice (MST bán + số bảo mật), nhưng
máy chủ riêng của PTP (ASP.NET). Khảo sát 27/10/2026 (đọc JS trên trang + gửi mã bịa):

    POST /Search/SearchInvoice  JSON {masothue, sobaomat, type:"pdf"}, Accept: application/pdf
      → 200 PDF                                 : thành công (JS mở blob PDF)
      → 417 text/plain                          : không tra được ở hệ thống mới → JS mở trang dự phòng
                                                  (27/10 thân 417 là lỗi kết nối SQL Server của chính
                                                  NCC, chờ ~30 s mới trả)
    GET  /Tracuu/ptp-Invoice/?mst=<MST bán>&id=<số bảo mật>   (hệ thống cũ, đường dự phòng)
      → 200 text/plain "Không tồn tại số bảo mật"   : không thấy → 'no_pdf'
    GET  /downloadXml.php?mst=&id=   → application/zip (lỗi thì thân là chữ báo lỗi, vẫn ghi zip)

Mã: TTKhac "Số bảo mật" (giống M-Invoice) / "Mã tra cứu"; MST người bán bắt buộc.
Chưa có HĐ thật (DB 27/10: 0 HĐ MSTTCGP 0100687474, 0 link mail) — chỉ kiểm phản hồi "không tìm thấy";
nhánh thành công dựng theo JS của trang (PDF trả thẳng), cần kiểm lại khi có HĐ thật.
"""
from __future__ import annotations

import io
import re
import zipfile
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .formlookup import lookup_code, pdf_from_response

BASE = "https://hoadondientu-ptp.vn"
CODE_KEYS = ("Số bảo mật", "Mã bảo mật", "sobaomat", "Mã tra cứu", "Mã tra cứu hóa đơn", "MTCuu")
NOT_FOUND = ("Không tồn tại", "không tồn tại", "Không tìm thấy")


class PtpAdapter(PdfAdapter):
    source_id = "ptp"
    name = "PTP-Invoice (hoadondientu-ptp.vn)"
    mst_tcgp = ("0100687474",)
    hosts = ("hoadondientu-ptp.vn",)

    @staticmethod
    def _keys(invoice: dict) -> tuple[str, str]:
        code = re.sub(r"[\s\-_/]", "", lookup_code(invoice, CODE_KEYS))   # trang cũng tự bỏ các ký tự này
        if not code:
            raise PdfFetchError("XML không có 'Số bảo mật' / mã tra cứu (PTP)", "no_code")
        mst = re.sub(r"[\s\-_/]", "", str(invoice.get("nbmst") or ""))
        if not mst:
            raise PdfFetchError("Thiếu MST người bán — PTP cần MST + số bảo mật", "no_code")
        return mst, code

    def fetch_pdf(self, invoice: dict) -> bytes:
        mst, code = self._keys(invoice)
        s = new_session()
        r = request(s, "POST", f"{BASE}/Search/SearchInvoice", timeout=60,
                    json={"masothue": mst, "sobaomat": code, "type": "pdf"},
                    headers={"Accept": "application/pdf", "Referer": f"{BASE}/tra-cuu/"})
        if r.status_code == 200 and is_pdf(r.content):
            check_pdf_is_invoice(r.content, invoice)
            return r.content
        if r.status_code not in (200, 417):
            raise PdfFetchError(f"PTP SearchInvoice trả HTTP {r.status_code}", "other")
        # 417 (hoặc 200 không phải PDF) → đường dự phòng như JS của trang
        r = request(s, "GET", f"{BASE}/Tracuu/ptp-Invoice/", params={"mst": mst, "id": code},
                    headers={"Referer": f"{BASE}/tra-cuu/"})
        if r.status_code != 200:
            raise PdfFetchError(f"PTP tra cứu dự phòng trả HTTP {r.status_code}", "other")
        if not is_pdf(r.content) and any(m in r.text for m in NOT_FOUND):
            raise PdfFetchError(f"PTP không tìm thấy HĐ (MST {mst}, số bảo mật {code})", "no_pdf")
        return pdf_from_response(s, r, invoice, self.name)

    def fetch_xml(self, invoice: dict) -> bytes:
        mst, code = self._keys(invoice)
        r = request(new_session(), "GET", f"{BASE}/downloadXml.php", params={"mst": mst, "id": code},
                    headers={"Referer": f"{BASE}/tra-cuu/"})
        if r.status_code != 200 or not zipfile.is_zipfile(io.BytesIO(r.content)):
            text = (r.content or b"")[:200].decode("utf-8", "replace")
            kind = "server" if "SQL Server" in text else "no_pdf"
            raise PdfFetchError(f"PTP downloadXml không trả ZIP: {text!r}", kind)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise PdfFetchError("PTP: ZIP không có file XML", "bad_content")
            xml = z.read(names[0])
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link dạng /Tracuu/ptp-Invoice/?mst=..&id=.. hoặc /tra-cuu/?mst=..&id=.. (chỉ đọc tham số)."""
        if not (urlsplit(url).hostname or "").endswith("hoadondientu-ptp.vn"):
            return None
        p = link_params(url)
        secret = p.get("id") or p.get("sobaomat") or (code or "").strip()
        mst = p.get("mst") or p.get("masothue")
        if not (secret and mst):
            return None
        return {"nbmst": mst, "ma_tra_cuu": secret, "tt_khac": {"Số bảo mật": secret}}


register(PtpAdapter())
