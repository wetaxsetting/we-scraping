"""
Phần mềm "iam-entry" (Moqui, cùng phần mềm với MS-INVOICE myinvoice.py và VININVOICE vininvoice.py), 2 cổng:
  - T-HOADON / TTC Invoice — MSTTCGP 0318511655 (Giải pháp doanh nghiệp T). Tra cứu https://tracuu.t-hoadon.vn/
  - Phần mềm IAM — MSTTCGP 0109193102. https://tracuu.iamsoftware.com.vn/ : trang gốc trả 404 (27/10/2026)
    nhưng API vẫn chạy (trả "không tìm thấy" đúng dạng) → vẫn làm adapter.

Không captcha, không cookie. SPA React gọi REST (base `/api/rest/s1`, khác `/erp/rest/s1` của myinvoice):
    GET /api/rest/s1/iam-entry/invoices/<mã>/header-info → 200 {"invoiceId","invoiceNo","invoiceTotal",...}
    GET /api/rest/s1/iam-entry/invoices/<mã>/pdf         → 200 application/pdf
    GET /api/rest/s1/iam-entry/invoices/<mã>/xml         → 200 XML có chữ ký
        mã sai → 400 {"errorCode":400,"errors":"Không tìm thấy hóa đơn hoặc hóa đơn đã bị xóa"}

Mỗi host một CSDL riêng: kiểm 27/10/2026 bằng MCCQT của 2 HĐ thật MS-INVOICE/VININVOICE — myinvoice.vn
thấy HĐ của nó, còn t-hoadon.vn và iamsoftware.com.vn đều "không tìm thấy". Như myinvoice/vininvoice,
cổng nhận MCCQT làm mã tra cứu → tra được từ dữ liệu Thuế; có mã riêng (ma_tra_cuu/TTKhac) thì thử trước.

Chưa có HĐ thật của 2 NCC này — chỉ kiểm phản hồi không tìm thấy.
"""
from __future__ import annotations

import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")


class IamEntryAdapter(PdfAdapter):
    """Cổng iam-entry tại `BASE` + `API_PATH` (mỗi host một CSDL)."""
    BASE = ""
    API_PATH = "/api/rest/s1/iam-entry/invoices"

    @staticmethod
    def lookup_codes(invoice: dict) -> list:
        """Mã tra cứu riêng (nếu có) trước, rồi MCCQT; bỏ trùng/rỗng/sai dạng (mã nằm trong URL)."""
        tt_khac = invoice.get("tt_khac") or {}
        cands = (invoice.get("ma_tra_cuu"), tt_khac.get("Mã tra cứu hóa đơn"), tt_khac.get("Mã tra cứu"),
                 tt_khac.get("MTCuu"), tt_khac.get("MaTraCuu"), invoice.get("ma_cqt"))
        codes = (str(c).strip() for c in cands if c and str(c).strip())
        return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]

    def _get(self, invoice: dict, kind: str):
        codes = self.lookup_codes(invoice)
        if not codes:
            raise PdfFetchError(f"Không có mã tra cứu lẫn MCCQT ({self.name})", "no_code")
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{self.BASE}{self.API_PATH}/{code}/{kind}",
                        headers={"Referer": f"{self.BASE}/"})
            if r.status_code in (400, 404):              # không tìm thấy → thử mã kế
                last = self._error_text(r) or f"không tìm thấy mã {code}"
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"{self.name} trả HTTP {r.status_code}", "other")
            return r
        raise PdfFetchError(f"{self.name} không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    @staticmethod
    def _error_text(r) -> str:
        try:
            return str((r.json() or {}).get("errors") or "").strip()
        except ValueError:
            return ""

    def fetch_pdf(self, invoice: dict) -> bytes:
        r = self._get(invoice, "pdf")
        if not is_pdf(r.content):
            raise PdfFetchError(f"{self.name} trả nội dung không phải PDF ({r.headers.get('Content-Type')})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        r = self._get(invoice, "xml")
        xml = r.content or b""
        head = xml.lstrip(b"\xef\xbb\xbf \r\n\t")[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError(f"{self.name} trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mã trong tham số link (nếu có) hoặc mã đi kèm trong mail. Không mở link."""
        p = link_params(url)
        cands = [p.get(k) for k in ("referencenumber", "code", "ma", "mtc", "matracuu", "lookupcode")] + [code]
        secret = next((c.strip() for c in cands if c and _CODE_RE.match(c.strip())), None)
        if not secret:
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


class THoaDonAdapter(IamEntryAdapter):
    source_id = "thoadon"
    name = "T-HOADON / TTC Invoice (tracuu.t-hoadon.vn)"
    mst_tcgp = ("0318511655",)
    hosts = ("tracuu.t-hoadon.vn",)
    BASE = "https://tracuu.t-hoadon.vn"


class IamSoftwareAdapter(IamEntryAdapter):
    source_id = "iamsoftware"
    name = "Phần mềm IAM (tracuu.iamsoftware.com.vn)"
    mst_tcgp = ("0109193102",)
    hosts = ("tracuu.iamsoftware.com.vn",)
    BASE = "https://tracuu.iamsoftware.com.vn"


register(THoaDonAdapter())
register(IamSoftwareAdapter())
