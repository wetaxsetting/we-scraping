# pdf_providers/ipos.py
"""
iPOS Invoice (Công ty CP IPOS.VN) — MSTTCGP 0104789847. Tra cứu: https://tracuuhoadon.ipos.vn/

Cổng chạy nền tra cứu CŨ của HILO (ASP.NET MVC, giống tracuuhddt78.hilo.com.vn). Form tra cứu
(POST /Invoice/search, "Mã nhận HĐ") đòi token reCAPTCHA v3 → KHÔNG dùng. Nhưng link trong
mail thông báo HĐ tải thẳng, không captcha, không cookie:

    token = base64("<khmshdon>:<khhdon>:<shdon>:<MST bán>")   vd "1:C26MYY:4177:0110765763"
    GET /Invoice/getinvoice?token=<token>
        → 200 application/pdf (filename 1C26MYY_00004177.pdf)  : thành công
        → 302 Location /Invoice/Index                        : không có HĐ
    GET /Invoice/ViewFromEmail?token=<token>  → HTML, có ajxCall4Portal('<id nội bộ>', ...)
    GET /Invoice/Download78?id=<id nội bộ>    → application/xml (XML 78, KHÔNG có chữ ký số)

Token chỉ là các trường công khai của HĐ (không có bí mật) → dựng được từ XML lẫn từ mail.
"Mã số bí mật" trong mail (ma_tra_cuu) chỉ dùng cho form có reCAPTCHA, không cần ở đây.
Rủi ro: iPOS đổi sang token có ký thì cách này hỏng (sẽ ra no_pdf hàng loạt).

Khảo sát 10/2026 trên HĐ thật GLAD PEOPLES 0110765763, 1C26MYY-4177 (HĐ máy tính tiền).
Theo khảo sát Excel, Haravan (e-invoices.haravan.app) và Pancake (tracuu.pancake.vn) cùng mẫu
giao diện; khi có HĐ thật để thử: kế thừa IposAdapter, đổi source_id/name/mst_tcgp/hosts/BASE.
"""
from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qs, urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

_ID_RE = re.compile(r"ajxCall4Portal\(\s*'(\d+)'")
_TOKEN_IN_TEXT_RE = re.compile(r"/Invoice/(?:getinvoice|ViewFromEmail)\?token=([A-Za-z0-9+/=_%-]+)", re.I)
_ID_IN_TEXT_RE = re.compile(r"/Invoice/Download78\?id=(\d+)", re.I)
# Mail iPOS: "Ký hiệu hóa đơn: 1C26MYY * Số hóa đơn: 00004177 ... MST: 0110765763"
_MAIL_KH_RE = re.compile(r"Ký hiệu hóa đơn:\s*(?:<[^>]+>\s*)*(\d)([CK]\d{2}[A-Z]{3})\b", re.I)
_MAIL_SO_RE = re.compile(r"Số hóa đơn:\s*(?:<[^>]+>\s*)*(\d{1,8})\b", re.I)
_MAIL_MST_RE = re.compile(r"Hóa Đơn Điện Tử của .{0,200}?MST:\s*(\d{10}(?:-\d{3})?)", re.I | re.S)


def _decode_token(token: str) -> dict | None:
    """token base64 → {khmshdon, khhdon, shdon, nbmst} hoặc None nếu không đúng dạng."""
    token = (token or "").strip()
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    parts = raw.split(":")
    if len(parts) != 4 or not (parts[0].isdigit() and parts[2].isdigit()) or not parts[3].strip():
        return None
    return {"khmshdon": int(parts[0]), "khhdon": parts[1].strip(), "shdon": int(parts[2]),
            "nbmst": parts[3].strip()}


class IposAdapter(PdfAdapter):
    source_id = "ipos"
    name = "iPOS Invoice (IPOS.VN)"
    mst_tcgp = ("0104789847",)
    hosts = ("ipos.vn",)
    BASE = "https://tracuuhoadon.ipos.vn"

    # -- mã tra cứu ----------------------------------------------------------

    @staticmethod
    def build_token(invoice: dict) -> str | None:
        """Token dựng từ trường HĐ (chịu được khmshdon/shdon dạng int hay chuỗi '00004177')."""
        khhdon = str(invoice.get("khhdon") or "").strip().upper()
        if len(khhdon) == 7 and khhdon[0].isdigit():          # "1C26MYY" → mẫu số dính kèm
            khhdon = khhdon[1:]
        try:
            khmshdon = int(str(invoice.get("khmshdon") or "1").strip())
            shdon = int(str(invoice.get("shdon") or "").strip())
        except ValueError:
            return None
        nbmst = str(invoice.get("nbmst") or "").strip()
        if not (khhdon and nbmst):
            return None
        return base64.b64encode(f"{khmshdon}:{khhdon}:{shdon}:{nbmst}".encode()).decode()

    def token(self, invoice: dict) -> str:
        tok = (invoice.get("ipos_token") or "").strip() or self.build_token(invoice)
        if not tok:
            raise PdfFetchError("Thiếu ký hiệu / số HĐ / MST bán để dựng token tra cứu iPOS", "no_code")
        return tok

    # -- link trong mail -------------------------------------------------------

    def invoice_from_link(self, url: str, code: str | None, mail_text: str) -> dict | None:
        """
        Nhận: /Invoice/getinvoice?token=..  và  /Invoice/ViewFromEmail?token=..  (token base64);
              /Invoice/Download78?id=..  (id nội bộ) — lấy token từ link kia trong cùng mail,
              không có thì đọc ký hiệu/số/MST trong nội dung mail.
        Không mở URL.
        """
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        q = {k.lower(): v[0] for k, v in parse_qs(parts.query).items() if v}
        path = parts.path.lower()
        mail_text = mail_text or ""
        token = ipos_id = None
        if path.endswith(("/invoice/getinvoice", "/invoice/viewfromemail")):
            token = q.get("token")
        elif path.endswith("/invoice/download78"):
            ipos_id = q.get("id") if (q.get("id") or "").isdigit() else None
            if not ipos_id:
                return None
            m = _TOKEN_IN_TEXT_RE.search(mail_text)
            token = m.group(1) if m else None
        else:
            return None                                     # trang chủ, /Invoice/Search, ảnh...
        fields = _decode_token(token) if token else None
        if not fields:
            fields = self._fields_from_mail(mail_text)
            token = None
        if not fields:
            return None
        if not ipos_id:
            m = _ID_IN_TEXT_RE.search(mail_text)
            ipos_id = m.group(1) if m else None
        code = (code or "").strip()
        if code == ipos_id:                                 # bộ trích link lấy nhầm id làm mã
            code = ""
        inv = {**fields, "mst_tcgp": self.mst_tcgp[0], "ma_tra_cuu": code or None,
               "lookup_url": url, "tt_khac": {}, "ipos_token": token or self.build_token(fields),
               "ipos_id": ipos_id}
        kh = inv["khhdon"]
        inv["loai_hd"] = "MTT" if len(kh) >= 4 and kh[3].upper() == "M" else None
        return inv

    @staticmethod
    def _fields_from_mail(text: str) -> dict | None:
        kh, so, mst = _MAIL_KH_RE.search(text), _MAIL_SO_RE.search(text), _MAIL_MST_RE.search(text)
        if not (kh and so and mst):
            return None
        return {"khmshdon": int(kh.group(1)), "khhdon": kh.group(2).upper(), "shdon": int(so.group(1)),
                "nbmst": mst.group(1)}

    # -- tải -------------------------------------------------------------------

    def fetch_pdf(self, invoice: dict) -> bytes:
        tok = self.token(invoice)
        s = new_session()
        r = request(s, "GET", f"{self.BASE}/Invoice/getinvoice", params={"token": tok},
                    headers={"Referer": f"{self.BASE}/"}, allow_redirects=False)
        if r.status_code in (301, 302, 303):
            raise PdfFetchError(
                f"iPOS không tìm thấy HĐ {invoice.get('khhdon')}-{invoice.get('shdon')} "
                f"của {invoice.get('nbmst')} (chuyển về {r.headers.get('Location')})", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"iPOS getinvoice trả HTTP {r.status_code}", "other")
        if not is_pdf(r.content):
            raise PdfFetchError(
                f"iPOS trả nội dung không phải PDF ({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        """XML 78 từ cổng (KHÔNG có chữ ký số — DSCKS rỗng). Kiểm đúng ký hiệu/số/MST bán."""
        s = new_session()
        ipos_id = str(invoice.get("ipos_id") or "").strip()
        if not ipos_id:
            r = request(s, "GET", f"{self.BASE}/Invoice/ViewFromEmail",
                        params={"token": self.token(invoice)},
                        headers={"Referer": f"{self.BASE}/"}, allow_redirects=False)
            m = _ID_RE.search(r.text or "") if r.status_code == 200 else None
            if not m:
                raise PdfFetchError(f"iPOS không tìm thấy HĐ (ViewFromEmail HTTP {r.status_code})",
                                    "no_pdf")
            ipos_id = m.group(1)
        r = request(s, "GET", f"{self.BASE}/Invoice/Download78", params={"id": ipos_id},
                    headers={"Referer": f"{self.BASE}/"}, allow_redirects=False)
        body = r.content or b""
        if r.status_code != 200 or b"<HDon" not in body[:2000]:
            raise PdfFetchError(f"iPOS Download78 id={ipos_id} không trả XML (HTTP {r.status_code})",
                                "no_pdf" if r.status_code in (200, 301, 302) else "other")
        self._check_xml(body, invoice)
        return body

    @staticmethod
    def _check_xml(xml: bytes, invoice: dict) -> None:
        text = xml.decode("utf-8", "replace")

        def tag(name):
            m = re.search(rf"<{name}>([^<]*)</{name}>", text)
            return (m.group(1) if m else "").strip()

        want_so = str(invoice.get("shdon") or "").strip().lstrip("0")
        want_kh = str(invoice.get("khhdon") or "").strip().upper()[-6:]
        want_mst = str(invoice.get("nbmst") or "").strip()
        got_so, got_kh = tag("SHDon").lstrip("0"), tag("KHHDon").upper()
        if (want_so and got_so != want_so) or (want_kh and got_kh != want_kh) or \
                (want_mst and f"<MST>{want_mst}</MST>" not in text):
            raise PdfFetchError(f"XML iPOS là HĐ khác ({got_kh}-{got_so})", "bad_content")


register(IposAdapter())
