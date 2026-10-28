"""
GO HÓA ĐƠN / Công nghệ Go Việt Nam — MSTTCGP 0111245245. Tra cứu: https://tracuu.gohoadon.vn/
Phần mềm VIET-INVOICE (iCORP) bán lại: bundle chọn giao diện theo hostname (PC = tracuuhoadon.
vietinvoice.vn, tracuupetro., tracuutuluc., tracuu.igas.vn, tracuu.gohoadon.vn), API ở host riêng.

Khảo sát 27/10/2026 (đọc /static/js/main.*.js). Không cookie, cần header x-domain = host trang tra cứu:
    GET https://hoadon.gohoadon.vn/api/v1/misc/find-by-lookup-code?lookupCode=<mã>
        → 200 {..., data: {no, serial, totalAfterTax, xmlString, ...}}   ("Tải hóa đơn XML" = xmlString)
        → 400 {"result":"failed","message":"Không tìm thấy thông tin"}
    GET .../api/v1/misc/export-by-lookup-code?lookupCode=<mã>&typeWebsite=1
        → {"fileURL": "/…pdf"} (tương đối → ghép https://hoadon.gohoadon.vn) = nút "Tải hóa đơn PDF"
        → 400 như trên khi không thấy
Bundle có khai recaptchaSiteKeyV2 nhưng không dùng ở luồng tra cứu (chỉ khai trong config);
gửi không token server trả bình thường. Link mail dạng ?lookupCode=<mã>.

Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy (400 "Không tìm thấy thông tin").
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .lookup_codes import lookup_codes

SITE_HOST = "tracuu.gohoadon.vn"
API_HOST = "https://hoadon.gohoadon.vn"
API = f"{API_HOST}/api/v1/misc"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
HEADERS = {"Accept": "application/json, text/plain, */*", "x-domain": SITE_HOST,
           "Origin": f"https://{SITE_HOST}", "Referer": f"https://{SITE_HOST}/"}


class GoHoaDonAdapter(PdfAdapter):
    source_id = "gohoadon"
    name = "GO HÓA ĐƠN (VIET-INVOICE)"
    mst_tcgp = ("0111245245",)
    hosts = (SITE_HOST,)

    @staticmethod
    def codes(invoice: dict) -> list:
        return lookup_codes(invoice, ("Mã tra cứu", "Mã tra cứu hóa đơn", "LookupCode", "lookupCode"),
                            _CODE_RE)

    def _first(self, invoice: dict, path: str, extra: dict | None = None):
        """(mã, JSON) của mã đầu tiên server nhận; 400 'không tìm thấy' → thử mã kế."""
        codes = self.codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu (GO HÓA ĐƠN)", "no_code")
        s = new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{API}/{path}", params={"lookupCode": code, **(extra or {})},
                        headers=HEADERS)
            try:
                js = r.json() or {}
            except ValueError:
                js = {}
            if r.status_code == 400:
                last = str(js.get("message") or "")
                continue
            if r.status_code != 200 or not isinstance(js, dict):
                raise PdfFetchError(f"GO HÓA ĐƠN trả HTTP {r.status_code}", "other")
            return s, code, js
        raise PdfFetchError(f"GO HÓA ĐƠN không tìm thấy HĐ ({', '.join(codes)}): {last}", "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        s, code, js = self._first(invoice, "export-by-lookup-code", {"typeWebsite": 1})
        data = js.get("data") if isinstance(js.get("data"), dict) else {}
        file_url = str(js.get("fileURL") or data.get("fileURL") or "").strip()
        if not file_url:
            raise PdfFetchError(f"GO HÓA ĐƠN không trả fileURL cho mã {code}", "no_pdf")
        r = request(s, "GET", urljoin(API_HOST + "/", file_url), headers={"Referer": f"https://{SITE_HOST}/"})
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(f"GO HÓA ĐƠN trả nội dung không phải PDF (HTTP {r.status_code}, "
                                f"{r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        _, code, js = self._first(invoice, "find-by-lookup-code")
        data = js.get("data") if isinstance(js.get("data"), dict) else {}
        xml = str(data.get("xmlString") or "").strip().encode("utf-8")
        if b"<HDon" not in xml[:5000]:
            raise PdfFetchError(f"GO HÓA ĐƠN không có XML cho mã {code}", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link ?lookupCode=<mã> hoặc mã trong mail. Không mở link."""
        secret = (link_params(url).get("lookupcode") or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


register(GoHoaDonAdapter())
