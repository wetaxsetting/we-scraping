# pdf_providers/kiotviet.py
"""
KiotViet — MSTTCGP 0104359717. Tra cứu: https://tracuuhoadon.kiotviet.vn/ (SPA Angular).

Không captcha, không cookie. SPA gọi REST ở https://api.einvoice.kiotviet.vn (khóa JSON dạng snake_case):
    POST /api/einvoice/search  {"signature": "<mã tra cứu>"}   (mã KHÔNG phân biệt hoa/thường)
        → 200 {"status":"success","data":{"pdf_file":{"link","name"},"pdf_xml_zip":{"link","name"}}}
          link = https://einvoice-search.kiotapi.com/api/v1/downloadPdfInvoice?code=HD02&id=<uuid>&b=1&y=26
          (HD02 = PDF, HD07 = ZIP {xml có chữ ký, pdf})
        → không tìm thấy: VẪN 200 "Tra cứu hóa đơn thành công" nhưng link rỗng ("")
    GET  /api/einvoice/download?<query của link>   → PDF / ZIP (đúng cách SPA tải: buildDownloadUrl)

Mã tra cứu (vd 81YGNI8VEF261479 = 10 ký tự + 2 số năm + số HĐ) chỉ in trên PDF / gửi trong mail. HĐ máy tính
tiền của KiotViet: XML Thuế không có MSTTCGP lẫn TTKhac → HĐ chỉ có XML Thuế ra `no_code`; đường chính là link
mail `https://tracuuhoadon.kiotviet.vn/?shd=<số HĐ>&mtc=<mã>` (invoice_from_link). Cổng không nhận MCCQT.

Khảo sát 27/10/2026: thật 1/1 qua link mail (C26MTT-1479, người bán 0319245420 Choryang BBQ Garden): PDF + ZIP
có XML ký số; mã viết thường cũng ra; mã bịa / MCCQT → link rỗng.
"""
from __future__ import annotations

import io
import re
import zipfile
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

PORTAL = "https://tracuuhoadon.kiotviet.vn"
API = "https://api.einvoice.kiotviet.vn"
_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,40}$")


class KiotVietAdapter(PdfAdapter):
    source_id = "kiotviet"
    name = "KiotViet (tracuuhoadon.kiotviet.vn)"
    mst_tcgp = ("0104359717",)
    hosts = ("tracuuhoadon.kiotviet.vn",)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for c in (invoice.get("ma_tra_cuu"), tt_khac.get("Mã tra cứu"), tt_khac.get("Mã tra cứu hóa đơn"),
                  tt_khac.get("MTCuu"), tt_khac.get("MaTraCuu")):
            c = str(c or "").strip()
            if _CODE_RE.match(c):
                return c
        return ""

    def _search(self, s, invoice: dict) -> dict:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("Không có mã tra cứu KiotViet (chỉ có trên PDF / mail)", "no_code")
        r = request(s, "POST", f"{API}/api/einvoice/search", json={"signature": code},
                    headers={"Origin": PORTAL, "Referer": f"{PORTAL}/", "Accept": "application/json"})
        if r.status_code == 404:
            raise PdfFetchError(f"KiotViet không tìm thấy HĐ (mã {code})", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"Tra cứu KiotViet trả HTTP {r.status_code}", "other")
        try:
            data = (r.json() or {}).get("data") or {}
        except ValueError as e:
            raise PdfFetchError("Tra cứu KiotViet trả nội dung không phải JSON", "bad_content") from e
        return data

    @staticmethod
    def download_url(link: str) -> str | None:
        """Như buildDownloadUrl của SPA: giữ query của link, đổi sang /api/einvoice/download."""
        q = urlsplit(link or "").query
        return f"{API}/api/einvoice/download?{q}" if q else None

    def _download(self, invoice: dict, key: str) -> bytes:
        s = new_session()
        data = self._search(s, invoice)
        url = self.download_url(((data.get(key) or {}).get("link") or "").strip())
        if not url:
            raise PdfFetchError(f"KiotViet không tìm thấy HĐ (mã {self.lookup_code(invoice)})", "no_pdf")
        r = request(s, "GET", url, headers={"Referer": f"{PORTAL}/"})
        if r.status_code == 404:
            raise PdfFetchError("KiotViet: link tải không còn file", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"Tải file KiotViet trả HTTP {r.status_code}", "other")
        return r.content or b""

    def fetch_pdf(self, invoice: dict) -> bytes:
        pdf = self._download(invoice, "pdf_file")
        if not is_pdf(pdf):
            raise PdfFetchError("KiotViet trả nội dung không phải PDF", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        """ZIP 'pdf_xml_zip' chứa XML có chữ ký người bán + MCCQT."""
        data = self._download(invoice, "pdf_xml_zip")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
                if name is None:
                    raise PdfFetchError("ZIP KiotViet không có file XML", "no_pdf")
                xml = zf.read(name)
        except zipfile.BadZipFile as e:
            raise PdfFetchError("KiotViet trả nội dung không phải ZIP", "bad_content") from e
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if not m:
            raise PdfFetchError("XML KiotViet không có SHDon", "bad_content")
        if shdon and int(m.group(1)) != int(shdon):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link mail `/?shd=<số>&mtc=<mã>` (hoặc trang chủ + mã trong mail). Không mở link."""
        p = link_params(url)
        mtc = p.get("mtc") or (code or "").strip()
        if not _CODE_RE.match(mtc):
            return None
        inv = {"ma_tra_cuu": mtc, "tt_khac": {"Mã tra cứu": mtc}}
        if (p.get("shd") or "").isdigit():
            inv["shdon"] = int(p["shd"])
        return inv


register(KiotVietAdapter())
