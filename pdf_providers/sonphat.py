# pdf_providers/sonphat.py
"""
Sơn Phát — MSTTCGP 4601328480. Tra cứu: https://tracuuhoadon.sonphat.vn/ (SPA React, API ở hoadon.sonphat.vn).

Không captcha (bundle có khai recaptchaSiteKeyV2 nhưng trang tra cứu không dùng), không cookie.
API https://hoadon.sonphat.vn/api/v1 (axios, interceptor trả thẳng body):
    GET /misc/find-by-lookup-code?lookupCode=<mã>
        → 200 {"result":"success","data":{"token",...}}
        → 404 {"result":"failed","message":["Không tìm thấy thông tin"]}
        → mã trùng nhiều HĐ: {"errorCode":"LOOKUP_AMBIGUOUS","candidates":[{token,no,serial,sellerName,...}]}
          (SPA cho người dùng chọn → adapter chọn theo số HĐ + ký hiệu)
    GET /misc/export-by-lookup-code?token=<token>   → PDF (blob)
    GET /misc/xml-by-token?token=<token>            → {"result":"success","data": <XML>}
Link mail/deeplink: https://tracuuhoadon.sonphat.vn/?lookupCode=<mã>.

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy (404 JSON như trên). Dạng chính xác
của "data" ở xml-by-token chưa thấy (SPA lưu thẳng thành file) → nhận chuỗi XML.
"""
from __future__ import annotations

import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

PORTAL = "https://tracuuhoadon.sonphat.vn"
API = "https://hoadon.sonphat.vn/api/v1"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_HEADERS = {"Origin": PORTAL, "Referer": f"{PORTAL}/", "Accept": "application/json, text/plain, */*"}


class SonPhatAdapter(PdfAdapter):
    source_id = "sonphat"
    name = "Sơn Phát (tracuuhoadon.sonphat.vn)"
    mst_tcgp = ("4601328480",)
    hosts = ("tracuuhoadon.sonphat.vn",)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for c in (invoice.get("ma_tra_cuu"), tt_khac.get("Mã tra cứu"), tt_khac.get("Mã tra cứu hóa đơn"),
                  tt_khac.get("MTCuu"), tt_khac.get("MaTraCuu"), tt_khac.get("LookupCode")):
            c = str(c or "").strip()
            if _CODE_RE.match(c):
                return c
        return ""

    @staticmethod
    def _json(r) -> dict:
        try:
            js = r.json()
        except ValueError:
            return {}
        return js if isinstance(js, dict) else {}

    @staticmethod
    def pick_candidate(cands: list, invoice: dict) -> dict | None:
        """Mã trùng nhiều HĐ → chọn đúng số HĐ (+ ký hiệu nếu có). Không chắc thì không chọn."""
        shdon = str(invoice.get("shdon") or "").strip()
        khhdon = str(invoice.get("khhdon") or "").strip().upper()
        if not shdon.isdigit():
            return None
        hits = [c for c in cands if isinstance(c, dict) and str(c.get("no") or "").strip().isdigit()
                and int(c["no"]) == int(shdon)]
        if khhdon and len(hits) > 1:
            hits = [c for c in hits if str(c.get("serial") or "").strip().upper().endswith(khhdon)]
        return hits[0] if len(hits) == 1 else None

    def _token(self, s, invoice: dict) -> str:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("Không có mã tra cứu Sơn Phát", "no_code")
        r = request(s, "GET", f"{API}/misc/find-by-lookup-code", params={"lookupCode": code}, headers=_HEADERS)
        js = self._json(r)
        if js.get("errorCode") == "LOOKUP_AMBIGUOUS":
            cand = self.pick_candidate(js.get("candidates") or [], invoice)
            if not cand or not cand.get("token"):
                raise PdfFetchError(f"Mã {code} trùng nhiều HĐ Sơn Phát, không chọn được HĐ đúng", "no_pdf")
            return str(cand["token"])
        if r.status_code == 404 or js.get("result") == "failed":
            raise PdfFetchError(f"Sơn Phát không tìm thấy HĐ (mã {code})", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"Tra cứu Sơn Phát trả HTTP {r.status_code}", "other")
        token = ((js.get("data") or {}) if isinstance(js.get("data"), dict) else {}).get("token")
        if not token:
            raise PdfFetchError("Tra cứu Sơn Phát không trả token", "bad_content")
        return str(token)

    def fetch_pdf(self, invoice: dict) -> bytes:
        s = new_session()
        token = self._token(s, invoice)
        r = request(s, "GET", f"{API}/misc/export-by-lookup-code", params={"token": token}, headers=_HEADERS)
        if r.status_code != 200:
            raise PdfFetchError(f"Tải PDF Sơn Phát trả HTTP {r.status_code}", "other")
        if not is_pdf(r.content):
            raise PdfFetchError("Sơn Phát trả nội dung không phải PDF", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        s = new_session()
        token = self._token(s, invoice)
        r = request(s, "GET", f"{API}/misc/xml-by-token", params={"token": token}, headers=_HEADERS)
        if r.status_code != 200:
            raise PdfFetchError(f"Tải XML Sơn Phát trả HTTP {r.status_code}", "other")
        data = self._json(r).get("data")
        if not isinstance(data, str) or "<" not in data:
            raise PdfFetchError("Sơn Phát không trả XML", "bad_content")
        xml = data.strip().encode("utf-8")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if not m:
            raise PdfFetchError("XML Sơn Phát không có SHDon", "bad_content")
        if shdon and int(m.group(1)) != int(shdon):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Deeplink `?lookupCode=<mã>` hoặc trang chủ + mã trong mail. Không mở link."""
        mtc = link_params(url).get("lookupcode") or (code or "").strip()
        if not _CODE_RE.match(mtc):
            return None
        return {"ma_tra_cuu": mtc, "tt_khac": {"Mã tra cứu": mtc}}


register(SonPhatAdapter())
