"""
Giải pháp số IGAS — MSTTCGP 0111090707. Tra cứu: https://tracuu.igas.vn/ (SPA React, ô "mã tra cứu";
link mail dạng `?lookupCode=<mã>`). Phần mềm là bản white-label của VIET-INVOICE (iCORP): cùng bundle
có cấu hình cho tracuuhoadon.vietinvoice.vn, tracuu.gohoadon.vn… nhưng bản build này gọi cứng API
https://hoadon.igas.vn/api/v1 (header `x-domain: tracuu.igas.vn`).

Khảo sát 27/10/2026 (đọc bundle main.*.js + gửi 3 mã bịa). Không captcha, không cookie:
    GET /misc/find-by-lookup-code?lookupCode=<mã>
      → 200 {"result":"success","data":{… "xmlString": "<HDon…>" …}}     : thấy HĐ (kèm XML)
      → 400 {"result":"failed","message":"Không tìm thấy thông tin"}        : 'no_pdf'
    GET /misc/export-by-lookup-code?lookupCode=<mã>&typeWebsite=1
      → {"fileURL": "/…pdf"}  → PDF ở https://hoadon.igas.vn<fileURL>  (trang hiện trong iframe)
Bundle có khai `recaptchaSiteKeyV2` trong cấu hình nhưng không chỗ nào dùng; server không đòi token.

Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy; dạng JSON thành công suy từ bundle.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
_KEY_HINTS = ("tracuu", "baomat", "mtcuu", "matc", "lookup", "searchkey")


def _norm_key(key: str) -> str:
    s = unicodedata.normalize("NFD", str(key)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def lookup_codes(invoice: dict) -> list:
    tt_khac = invoice.get("tt_khac") or {}
    cands = [invoice.get("ma_tra_cuu")]
    cands += [v for k, v in tt_khac.items() if any(h in _norm_key(k) for h in _KEY_HINTS)]
    codes = (str(c).strip() for c in cands if c and str(c).strip())
    return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]


class IgasAdapter(PdfAdapter):
    source_id = "igas"
    name = "IGAS (tracuu.igas.vn, phần mềm VIET-INVOICE)"
    mst_tcgp = ("0111090707",)
    hosts = ("tracuu.igas.vn", "hoadon.igas.vn")
    API_BASE = "https://hoadon.igas.vn"
    LOOKUP_HOST = "tracuu.igas.vn"

    def _headers(self) -> dict:
        return {"x-domain": self.LOOKUP_HOST, "Origin": f"https://{self.LOOKUP_HOST}",
                "Referer": f"https://{self.LOOKUP_HOST}/", "Accept": "application/json, text/plain, */*"}

    def _get_json(self, s, path: str, code: str, **params) -> dict | None:
        r = request(s, "GET", f"{self.API_BASE}/api/v1{path}", params={"lookupCode": code, **params},
                    headers=self._headers())
        if r.status_code in (400, 404):
            return None
        if r.status_code != 200:
            raise PdfFetchError(f"{self.name} trả HTTP {r.status_code} ({path})", "other")
        try:
            return r.json() or {}
        except ValueError as e:
            raise PdfFetchError(f"{self.name}: {path} không trả JSON", "bad_content") from e

    def _find(self, s, invoice: dict) -> tuple:
        codes = lookup_codes(invoice)
        if not codes:
            raise PdfFetchError(f"Không có mã tra cứu ({self.name})", "no_code")
        for code in codes[:3]:
            js = self._get_json(s, "/misc/find-by-lookup-code", code)
            if js and isinstance(js.get("data"), dict) and js.get("result") != "failed":
                return code, js["data"]
        raise PdfFetchError(f"{self.name} không tìm thấy HĐ ({', '.join(codes[:3])})", "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        s = new_session()
        code, _ = self._find(s, invoice)
        js = self._get_json(s, "/misc/export-by-lookup-code", code, typeWebsite=1)
        file_url = ((js or {}).get("fileURL") or ((js or {}).get("data") or {}).get("fileURL") or "")
        if not file_url:
            raise PdfFetchError(f"{self.name}: không lấy được đường dẫn PDF cho mã {code}", "no_pdf")
        r = request(s, "GET", urljoin(self.API_BASE + "/", file_url), headers=self._headers())
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(f"{self.name}: file tải về không phải PDF (HTTP {r.status_code})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        s = new_session()
        _, data = self._find(s, invoice)
        xml = (data.get("xmlString") or "").strip().encode("utf-8")
        if not xml:
            raise PdfFetchError(f"{self.name}: kết quả tra cứu không có XML", "no_pdf")
        head = xml.lstrip(b"\xef\xbb\xbf")[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError(f"{self.name} trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link `?lookupCode=<mã>` hoặc mã đi kèm trong mail. Không mở link."""
        secret = (link_params(url).get("lookupcode") or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


register(IgasAdapter())
