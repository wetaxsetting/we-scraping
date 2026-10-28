# pdf_providers/kimtuthap.py
"""
Kim Tự Tháp (PSCForm e-Invoice Services) — MSTTCGP 0303549303. Tra cứu: https://e-invoices.vn/User/Search

ASP.NET Core, không captcha, không cookie bắt buộc. Form GET:
    GET /User/Search?CurrSecretCodeSearchStr=<Secret code — GUID đúng 36 ký tự>
        → không tìm thấy: trang có "Invalid secret code!" (validation-summary-errors)
        → thấy HĐ: script cuối trang gắn nút #aViewPdf mở <iframe id="pdfViewer" data-src="<URL PDF>">
          → adapter lấy data-src của #pdfViewer (hoặc link khác có "pdf" trong kết quả) rồi GET.
Mã: GUID 36 ký tự (ma_tra_cuu hoặc bất kỳ TTKhac nào có dạng GUID).

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy ("Invalid secret code!" cho cả GUID 0 lẫn GUID
ngẫu nhiên). URL PDF lấy theo cấu trúc trang (data-src), chưa thấy giá trị thật.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urljoin

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://e-invoices.vn"
_GUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
_VIEWER_RE = re.compile(r"<iframe\b[^>]*\bid=\"pdfViewer\"[^>]*>", re.I)
_DATA_SRC_RE = re.compile(r"\bdata-src=\"([^\"]+)\"", re.I)
_PDF_LINK_RE = re.compile(r"\b(?:href|data-src|src)=\"([^\"]*pdf[^\"]*)\"", re.I)


class KimTuThapAdapter(PdfAdapter):
    source_id = "kimtuthap"
    name = "Kim Tự Tháp (e-invoices.vn)"
    mst_tcgp = ("0303549303",)
    hosts = ("e-invoices.vn",)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for c in [invoice.get("ma_tra_cuu")] + list(tt_khac.values()):
            c = str(c or "").strip()
            if _GUID_RE.match(c):
                return c
        return ""

    @staticmethod
    def pdf_url(page: str) -> str | None:
        """URL PDF trên trang kết quả: data-src của iframe#pdfViewer, không có thì link có 'pdf'."""
        m = _VIEWER_RE.search(page)
        if m:
            d = _DATA_SRC_RE.search(m.group(0))
            if d:
                return urljoin(f"{BASE}/", html.unescape(d.group(1)))
        main = page.split("<main", 1)[-1].split("</main>", 1)[0]
        for u in _PDF_LINK_RE.findall(main):
            u = html.unescape(u)
            if u.startswith(("#", "javascript:")) or u.lower().endswith(".css"):
                continue
            return urljoin(f"{BASE}/", u)
        return None

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("Không có Secret code (GUID) Kim Tự Tháp", "no_code")
        s = new_session()
        r = request(s, "GET", f"{BASE}/User/Search", params={"CurrSecretCodeSearchStr": code},
                    headers={"Referer": f"{BASE}/User/Search"})
        if r.status_code != 200:
            raise PdfFetchError(f"Tra cứu Kim Tự Tháp trả HTTP {r.status_code}", "other")
        if "Invalid secret code" in r.text:
            raise PdfFetchError(f"Kim Tự Tháp không tìm thấy HĐ (mã {code})", "no_pdf")
        url = self.pdf_url(r.text)
        if not url:
            raise PdfFetchError("Trang kết quả Kim Tự Tháp không có link PDF", "bad_content")
        p = request(s, "GET", url, headers={"Referer": r.url if isinstance(getattr(r, "url", None), str)
                                            else f"{BASE}/User/Search"})
        if p.status_code != 200:
            raise PdfFetchError(f"Tải PDF Kim Tự Tháp trả HTTP {p.status_code}", "other")
        if not is_pdf(p.content):
            raise PdfFetchError("Kim Tự Tháp trả nội dung không phải PDF", "bad_content")
        check_pdf_is_invoice(p.content, invoice)
        return p.content

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link `/User/Search?CurrSecretCodeSearchStr=<GUID>` hoặc mã GUID trong mail. Không mở link."""
        sid = link_params(url).get("currsecretcodesearchstr") or (code or "").strip()
        if not _GUID_RE.match(sid):
            return None
        return {"ma_tra_cuu": sid, "tt_khac": {"Secret code": sid}}


register(KimTuThapAdapter())
