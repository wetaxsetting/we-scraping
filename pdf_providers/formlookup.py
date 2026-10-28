"""
Hàm dùng chung cho các cổng tra cứu kiểu "form 1–2 ô, không captcha" (lô C2, 27/10/2026):
ptp, acman, avse, koffi, vietinfo, ehcm.

- `lookup_code`: lấy mã tra cứu từ dict HĐ (TTKhac theo danh sách khóa, rồi `ma_tra_cuu`).
- `hidden_fields`: các ô ẩn của trang ASP.NET WebForms (__VIEWSTATE, __EVENTVALIDATION...).
- `pdf_from_response`: phản hồi tra cứu → PDF. Cổng trả thẳng PDF thì dùng luôn; trả HTML thì
  tìm đúng 1 link/iframe/object trỏ tới PDF trên cùng host và tải 1 lần. Không bao giờ trả bytes
  không phải PDF.
"""
from __future__ import annotations

import html as _html
import re
from urllib.parse import urljoin, urlsplit

from .base import PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, request

DEFAULT_CODE_KEYS = ("Mã tra cứu", "Mã tra cứu hóa đơn", "MaTraCuu", "MTCuu", "Mã TC", "Số bảo mật",
                     "Mã bảo mật", "sobaomat", "Mã hóa đơn", "Fkey", "FKey")

_HIDDEN_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([\w:$-]+)\s*=\s*"([^"]*)"')
_PDF_LINK_RE = re.compile(r'(?:href|src|data)\s*=\s*["\']([^"\']*?(?:\.pdf\b|[?&](?:type|format)=pdf\b)[^"\']*)["\']',
                          re.I)


def lookup_code(invoice: dict, keys=DEFAULT_CODE_KEYS) -> str:
    """Mã tra cứu: TTKhac theo thứ tự `keys` (so khớp không phân biệt hoa/thường), rồi ma_tra_cuu."""
    tt_khac = {str(k).strip().lower(): v for k, v in (invoice.get("tt_khac") or {}).items()}
    for key in keys:
        val = str(tt_khac.get(key.lower()) or "").strip()
        if val:
            return val
    return str(invoice.get("ma_tra_cuu") or "").strip()


def hidden_fields(page: str) -> dict:
    """<input type="hidden" name=... value=...> → dict (thứ tự thuộc tính tùy ý)."""
    out = {}
    for tag in _HIDDEN_RE.findall(page or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        if attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
            out[attrs["name"]] = _html.unescape(attrs.get("value", ""))
    return out


def pdf_link(page: str, base_url: str) -> str | None:
    """Link PDF đầu tiên trong HTML, chỉ nhận link cùng host với trang (không đi sang site lạ)."""
    host = urlsplit(base_url).hostname
    for raw in _PDF_LINK_RE.findall(page or ""):
        url = urljoin(base_url, _html.unescape(raw).strip())
        if urlsplit(url).hostname == host:
            return url
    return None


def pdf_from_response(s, r, invoice: dict, name: str) -> bytes:
    """Phản hồi tra cứu (đã biết KHÔNG phải 'không tìm thấy') → PDF đã kiểm, hoặc PdfFetchError."""
    if is_pdf(r.content):
        check_pdf_is_invoice(r.content, invoice)
        return r.content
    link = pdf_link(r.text, r.url if isinstance(getattr(r, "url", None), str) else "")
    if not link:
        raise PdfFetchError(f"{name}: phản hồi tra cứu không có PDF lẫn link PDF "
                            f"({r.headers.get('Content-Type')}) — cần khảo sát lại trên HĐ thật",
                            "bad_content")
    r2 = request(s, "GET", link, headers={"Referer": r.url})
    if r2.status_code != 200 or not is_pdf(r2.content):
        raise PdfFetchError(f"{name}: link {link} không trả PDF (HTTP {r2.status_code})", "bad_content")
    check_pdf_is_invoice(r2.content, invoice)
    return r2.content
