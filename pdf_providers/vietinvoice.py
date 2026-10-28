"""
VIET-INVOICE / CTCP iCORP — MSTTCGP 0106870211. iCORP có 2 cổng tra cứu, cùng tên miền vietinvoice.vn:

1. Bản mới (SPA React, white-label dùng chung với GO HÓA ĐƠN, IGAS — xem gohoadon.py / igas.py):
   trang https://tracuuhoadon.vietinvoice.vn/ , API https://hoadondientu.vietinvoice.vn/api/v1
   (header bắt buộc `x-domain: tracuuhoadon.vietinvoice.vn`, axios đặt = hostname trang):
       GET /misc/find-by-lookup-code?lookupCode=<mã>
           → 200 {"result":"success","data":{…, "xmlString": "<HDon…>"}}
           → 400 {"result":"failed","message":"Không tìm thấy thông tin"}
       GET /misc/export-by-lookup-code?lookupCode=<mã>&typeWebsite=1 → {"fileURL": "/…pdf"} (ghép API host)
   Link mail dạng `?lookupCode=<mã>`. Bundle có khai recaptchaSiteKeyV2 trong cấu hình nhưng luồng tra cứu
   không dùng; server trả 400 "không tìm thấy" bình thường khi không có token.
2. Bản cũ https://tracuu.vietinvoice.vn/#/ (link trong khảo sát 14/10): cùng phần mềm VININVOICE /
   MS-INVOICE (xem vininvoice.py) — GET /erp/rest/s1/iam-entry/invoices/<mã>/{header-info,pdf,xml},
   mã sai → 400 {"errors":"Không tìm thấy hóa đơn có mã tra cứu: …"}. Bên VININVOICE cổng này nhận cả
   MCCQT nên ở đây cũng thử MCCQT sau cùng (chưa kiểm được với iCORP).

Thử cổng mới trước (mã tra cứu), không thấy thì cổng cũ (mã tra cứu rồi MCCQT). Không captcha, không cookie.

Khảo sát 27/10/2026 (đọc bundle 2 cổng). Đã kiểm thật đến đâu: chưa có HĐ thật (DB 27/10: 0 HĐ MSTTCGP
0106870211, 0 link mail) — chỉ kiểm phản hồi "không tìm thấy" (400 JSON) của cả 2 cổng bằng mã bịa.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin

from . import register
from .base import PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request
from .vininvoice import VinInvoiceAdapter

OLD_BASE = "https://tracuu.vietinvoice.vn"
OLD_API = f"{OLD_BASE}/erp/rest/s1/iam-entry/invoices"
NEW_SITE = "tracuuhoadon.vietinvoice.vn"
NEW_API_HOST = "https://hoadondientu.vietinvoice.vn"
NEW_API = f"{NEW_API_HOST}/api/v1/misc"
NEW_HEADERS = {"Accept": "application/json, text/plain, */*", "x-domain": NEW_SITE,
               "Origin": f"https://{NEW_SITE}", "Referer": f"https://{NEW_SITE}/"}
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_KEY_HINTS = ("tracuu", "mtcuu", "lookup", "baomat")


def _norm(key: str) -> str:
    s = unicodedata.normalize("NFD", str(key)).replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in s if c.isalnum() and not unicodedata.combining(c)).lower()


class VietInvoiceAdapter(VinInvoiceAdapter):
    source_id = "vietinvoice"
    name = "VIET-INVOICE / iCORP (tracuuhoadon.vietinvoice.vn, tracuu.vietinvoice.vn)"
    mst_tcgp = ("0106870211",)
    hosts = ("tracuuhoadon.vietinvoice.vn", "tracuu.vietinvoice.vn", "hoadondientu.vietinvoice.vn")

    @staticmethod
    def secret_codes(invoice: dict) -> list:
        """Mã tra cứu riêng (không gồm MCCQT): ma_tra_cuu + TTKhac có tên giống 'mã tra cứu'."""
        cands = [invoice.get("ma_tra_cuu")]
        cands += [v for k, v in (invoice.get("tt_khac") or {}).items()
                  if any(h in _norm(k) for h in _KEY_HINTS)]
        codes = (str(c).strip() for c in cands if c and str(c).strip())
        return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]

    # ---------------------------------------------------------------- cổng mới
    def _new_find(self, s, invoice: dict):
        """(mã, data) ở cổng mới, hoặc None nếu cổng mới không có mã nào."""
        for code in self.secret_codes(invoice)[:3]:
            r = request(s, "GET", f"{NEW_API}/find-by-lookup-code", params={"lookupCode": code},
                        headers=NEW_HEADERS)
            if r.status_code in (400, 404):
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"VIET-INVOICE trả HTTP {r.status_code} (find-by-lookup-code)", "other")
            try:
                js = r.json() or {}
            except ValueError as e:
                raise PdfFetchError("VIET-INVOICE: find-by-lookup-code không trả JSON", "bad_content") from e
            if isinstance(js.get("data"), dict) and js.get("result") != "failed":
                return code, js["data"]
        return None

    def _new_pdf(self, s, code: str, invoice: dict) -> bytes:
        r = request(s, "GET", f"{NEW_API}/export-by-lookup-code", params={"lookupCode": code, "typeWebsite": 1},
                    headers=NEW_HEADERS)
        js = {}
        if r.status_code == 200:
            try:
                js = r.json() or {}
            except ValueError:
                js = {}
        data = js.get("data") if isinstance(js.get("data"), dict) else {}
        file_url = str(js.get("fileURL") or data.get("fileURL") or "").strip()
        if not file_url:
            raise PdfFetchError(f"VIET-INVOICE không trả fileURL cho mã {code} (HTTP {r.status_code})", "no_pdf")
        r = request(s, "GET", urljoin(NEW_API_HOST + "/", file_url), headers=NEW_HEADERS)
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(f"VIET-INVOICE: file tải về không phải PDF (HTTP {r.status_code}, "
                                f"{r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    # ---------------------------------------------------------------- cổng cũ (phần mềm VININVOICE)
    def _get(self, invoice: dict, kind: str, s=None):
        codes = self.lookup_codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu lẫn MCCQT (VIET-INVOICE)", "no_code")
        s = s or new_session()
        last = ""
        for code in codes:
            r = request(s, "GET", f"{OLD_API}/{code}/{kind}", headers={"Referer": f"{OLD_BASE}/"})
            if r.status_code == 400:                    # không tìm thấy → thử mã kế
                last = self._error_text(r) or f"không tìm thấy mã {code}"
                continue
            if r.status_code != 200:
                raise PdfFetchError(f"VIET-INVOICE (cổng cũ) trả HTTP {r.status_code}", "other")
            return r
        raise PdfFetchError(f"VIET-INVOICE không tìm thấy HĐ ở cả 2 cổng ({', '.join(codes)}): {last}",
                            "no_pdf")

    @staticmethod
    def lookup_codes(invoice: dict) -> list:
        codes = VietInvoiceAdapter.secret_codes(invoice) + VinInvoiceAdapter.lookup_codes(invoice)
        return [c for c in dict.fromkeys(codes) if re.match(r"^[A-Za-z0-9]{8,40}$", c)]

    # ---------------------------------------------------------------- API adapter
    def _old_or_not_found(self, invoice: dict) -> None:
        """Cổng mới không thấy và không có mã nào hợp dạng cổng cũ → no_pdf (không phải no_code)."""
        if not self.lookup_codes(invoice):
            raise PdfFetchError(f"VIET-INVOICE không tìm thấy HĐ ({', '.join(self.secret_codes(invoice))})",
                                "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        if not self.lookup_codes(invoice) and not self.secret_codes(invoice):
            raise PdfFetchError("Không có mã tra cứu lẫn MCCQT (VIET-INVOICE)", "no_code")
        s = new_session()
        found = self._new_find(s, invoice)
        if found:
            return self._new_pdf(s, found[0], invoice)
        self._old_or_not_found(invoice)
        r = self._get(invoice, "pdf", s)
        if not is_pdf(r.content):
            raise PdfFetchError(f"VIET-INVOICE (cổng cũ) trả nội dung không phải PDF "
                                f"({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        if not self.lookup_codes(invoice) and not self.secret_codes(invoice):
            raise PdfFetchError("Không có mã tra cứu lẫn MCCQT (VIET-INVOICE)", "no_code")
        s = new_session()
        found = self._new_find(s, invoice)
        if found:
            xml = str(found[1].get("xmlString") or "").strip().encode("utf-8")
        else:
            self._old_or_not_found(invoice)
            xml = self._get(invoice, "xml", s).content or b""
        head = xml.lstrip(b"\xef\xbb\xbf").lstrip()[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError("VIET-INVOICE trả nội dung không phải XML", "bad_content")
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


register(VietInvoiceAdapter())
