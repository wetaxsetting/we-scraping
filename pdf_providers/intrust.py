"""
INTRUST — Dịch vụ Chữ ký số INTRUST (MSTTCGP 0110333410) và SCHOOL TECH (0317509886): cả hai dùng chung
cổng tra cứu https://lookupeinvoice.intrustdss.vn/ (AngularJS 1.x, ô duy nhất "Nhập mã tra cứu").

Khảo sát 27/10/2026 (đọc JS + gửi 4 mã bịa):
    POST /kiem-tra/check  JSON {certificate_id, signature: <mã tra cứu>, gRecaptchaResponse, seller_tax_code}
      → {"status":"error","code":"HE035","message":"Thông tin tra cứu không đúng ..."}  : không thấy → 'no_pdf'
      → {"status":"success","data":{id, invoice_number, template_code, invoice_series, payment_status,
         check_payment_status, pdf_file:{link}, attach_file:[{code,name,link}], compress_file:{link}}}
    attach_file.code: HD01 = XML, HD02 = PDF (theo template view.php của cổng).
    Không cần cookie. Form trên trang chỉ gửi `signature` (MST/số HĐ chỉ có khi mở link
    `/?mst=&shd=&mtc=` trong mail) nên adapter gửi giống form.

Trang có nạp angular-recaptcha, nhưng site key là chuỗi giả ('sdffsfs') và server KHÔNG đòi token:
gửi `gRecaptchaResponse: null` vẫn nhận câu trả lời "không đúng" bình thường (4/4 lượt). Controller chỉ bật
reCAPTCHA khi server trả `require_captcha: 1` — gặp thì adapter dừng ('rate_limit', thử lại sau),
KHÔNG giải reCAPTCHA.

Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy; dạng JSON thành công suy từ template của cổng.
Mã tra cứu: XML chưa thấy mẫu → lấy ma_tra_cuu, rồi TTKhac có tên kiểu "mã tra cứu"/"mã bảo mật".
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://lookupeinvoice.intrustdss.vn/"
_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
_KEY_HINTS = ("tracuu", "baomat", "mtcuu", "matc", "lookup", "searchkey", "secret", "signature")


def _norm_key(key: str) -> str:
    s = unicodedata.normalize("NFD", str(key)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def lookup_codes(invoice: dict) -> list:
    """ma_tra_cuu trước, rồi TTKhac có tên giống mã tra cứu; bỏ trùng/rỗng/sai dạng."""
    tt_khac = invoice.get("tt_khac") or {}
    cands = [invoice.get("ma_tra_cuu")]
    cands += [v for k, v in tt_khac.items() if any(h in _norm_key(k) for h in _KEY_HINTS)]
    codes = (str(c).strip() for c in cands if c and str(c).strip())
    return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]


class IntrustAdapter(PdfAdapter):
    source_id = "intrust"
    name = "INTRUST / SCHOOL TECH (lookupeinvoice.intrustdss.vn)"
    mst_tcgp = ("0110333410", "0317509886")
    hosts = ("lookupeinvoice.intrustdss.vn",)

    def _check(self, s, invoice: dict) -> dict:
        codes = lookup_codes(invoice)
        if not codes:
            raise PdfFetchError(f"Không có mã tra cứu ({self.name})", "no_code")
        last = ""
        for code in codes[:3]:
            r = request(s, "POST", urljoin(BASE, "kiem-tra/check"),
                        json={"certificate_id": "", "signature": code,
                              "gRecaptchaResponse": None, "seller_tax_code": ""},
                        headers={"Referer": BASE, "Origin": BASE.rstrip("/")})
            if r.status_code != 200:
                raise PdfFetchError(f"{self.name} trả HTTP {r.status_code}", "other")
            try:
                js = r.json() or {}
            except ValueError as e:
                raise PdfFetchError(f"{self.name}: trả lời không phải JSON", "bad_content") from e
            data = js.get("data") if isinstance(js.get("data"), dict) else {}
            if js.get("status") == "success" and data:
                return data
            if js.get("require_captcha") == 1 or data.get("require_captcha") == 1:
                raise PdfFetchError(f"{self.name} đòi reCAPTCHA — không vượt, thử lại sau", "rate_limit")
            last = str(js.get("message") or js.get("code") or "")
        raise PdfFetchError(f"{self.name} không tìm thấy HĐ ({', '.join(codes[:3])}): {last}", "no_pdf")

    @staticmethod
    def _links(data: dict, code: str) -> list:
        links = [f.get("link") for f in (data.get("attach_file") or [])
                 if isinstance(f, dict) and f.get("code") == code and f.get("link")]
        if code == "HD02":
            links.append((data.get("pdf_file") or {}).get("link"))
        return [urljoin(BASE, x) for x in dict.fromkeys(links) if x]

    @staticmethod
    def _unpaid(data: dict) -> bool:
        return bool(data.get("check_payment_status")) and not data.get("payment_status")

    def _download(self, invoice: dict, code: str, label: str) -> bytes:
        s = new_session()
        data = self._check(s, invoice)
        if self._unpaid(data):
            raise PdfFetchError(f"{self.name}: HĐ chưa thanh toán, cổng không cho tải", "no_pdf")
        links = self._links(data, code)
        if not links:
            raise PdfFetchError(f"{self.name}: kết quả tra cứu không có file {label}", "no_pdf")
        for url in links:
            r = request(s, "GET", url, headers={"Referer": BASE})
            if r.status_code == 200 and r.content:
                return r.content
        raise PdfFetchError(f"{self.name}: không tải được file {label}", "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        pdf = self._download(invoice, "HD02", "PDF")
        if not is_pdf(pdf):
            raise PdfFetchError(f"{self.name} trả nội dung không phải PDF", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        xml = self._download(invoice, "HD01", "XML")
        head = xml.lstrip(b"\xef\xbb\xbf \r\n\t")[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError(f"{self.name} trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link mail dạng /?mst=<MST bán>&shd=<số HĐ>&mtc=<mã>. Không mở link."""
        if not (urlsplit(url).hostname or "").lower().endswith("intrustdss.vn"):
            return None
        p = link_params(url)
        secret = (p.get("mtc") or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        inv = {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}
        if p.get("mst"):
            inv["nbmst"] = p["mst"]
        if (p.get("shd") or "").isdigit():
            inv["shdon"] = int(p["shd"])
        return inv


register(IntrustAdapter())
