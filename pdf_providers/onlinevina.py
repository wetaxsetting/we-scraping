"""
Online VI NA (Vina Invoice) — MSTTCGP 0314185087. Tra cứu: https://hoadon.onlinevina.com.vn/invoice
(SPA React; API https://hoadon.onlinevina.com.vn/api/v1/...).

Mã tra cứu: TTKhac "SearchInvoice" = 24 ký tự hex (vd 6604fc6cfef036498fbc01ad), cũng in trên PDF
("Mã tra cứu : ..."). Trang tra cứu nhận cả ?id=<mã>.

Quy trình (khảo sát + kiểm trên HĐ thật 26/10/2026):
  POST /api/v1/invoice/invoice/view  JSON {"search_invoice_id": <mã>}
    → {"code": 50000, "data": {"xml_content": <TDiep 202 của CQT, chứa HDon có MCCQT + chữ ký>,
                               "xslt_content": <mẫu XSLT của người bán>, "status", "state_of_bill"}}
    → {"code": 51202, "message": "Không tồn tại ID tìm kiếm hoá đơn"}
Không captcha, không cookie.

Server KHÔNG có PDF: nút ".PDF" trên trang chụp HTML bằng html2pdf ngay trên trình duyệt. Ta làm như
trang: tách <HDon> khỏi TDiep (đúng như viewXSLT của trang), áp XSLT bằng lxml (cấm mọi truy cập
file/mạng của XSLT), rồi dựng PDF bằng Chromium (render.py, tắt JS, chặn mạng). Mẫu XSLT để sẵn
nội dung HĐ trong khối ẩn rồi dùng JS chia trang → ta bỏ JS và bỏ các "display:none" (trừ ảnh nền),
nên PDF không có viền bảng/ảnh nền nhưng đủ chữ: trùng 100% chữ PDF người bán gửi qua mail
(kiểm 1/1, so bỏ khoảng trắng). Cần lxml (pip install lxml).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://hoadon.onlinevina.com.vn"
PAGE = BASE + "/invoice"
VIEW = BASE + "/api/v1/invoice/invoice/view"
OK, NOT_FOUND = 50000, 51202
_CODE_RE = re.compile(r"^[0-9a-fA-F]{24}$")
_HDON_RE = re.compile(r"<HDon>.*</HDon>", re.S)
_SHDON_RE = re.compile(r"<SHDon>\s*0*(\d+)\s*</SHDon>")
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
# style="display:none" trên mọi thẻ div, trừ khối ảnh nền id="background"
_HIDDEN_RE = re.compile(r'(<div)(?![^>]*id="background")([^>]*?)\sstyle="\s*display:\s*none;?\s*"', re.I)


def xslt_to_html(xml: str, xslt: str) -> str:
    """Áp XSLT của người bán lên <HDon>. XSLT là dữ liệu bên thứ ba → cấm đọc file / mạng."""
    try:
        from lxml import etree
    except ImportError as e:
        raise PdfFetchError("Thiếu lxml (pip install lxml) để dựng HĐ Online VI NA", "other") from e
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    try:
        style = etree.fromstring(xslt.encode("utf-8"), parser)
        transform = etree.XSLT(style, access_control=etree.XSLTAccessControl.DENY_ALL)
        out = transform(etree.fromstring(xml.encode("utf-8"), parser))
    except (etree.XMLSyntaxError, etree.XSLTError) as e:
        raise PdfFetchError(f"Không áp được mẫu XSLT Online VI NA: {e}", "bad_content") from e
    return str(out)


def printable(page_html: str) -> str:
    """Bỏ JS chia trang + hiện khối nội dung ẩn (JS của mẫu vốn chép chúng ra trang)."""
    return _HIDDEN_RE.sub(r"\1\2", _SCRIPT_RE.sub("", page_html))


def _pdf_text(pdf: bytes) -> str:
    try:
        import fitz
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            return "".join("".join(p.get_text() for p in doc).split())
    except Exception:  # noqa: BLE001
        return ""


class OnlineVinaAdapter(PdfAdapter):
    source_id = "onlinevina"
    name = "Online VI NA (hoadon.onlinevina.com.vn)"
    mst_tcgp = ("0314185087",)
    hosts = ("hoadon.onlinevina.com.vn",)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for v in (tt_khac.get("SearchInvoice"), tt_khac.get("Mã tra cứu"), invoice.get("ma_tra_cuu")):
            if v and str(v).strip():
                return str(v).strip()
        return ""

    def _view(self, invoice: dict) -> dict:
        """→ data {xml_content, xslt_content, ...} đã kiểm đúng số HĐ."""
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có TTKhac 'SearchInvoice' (mã tra cứu Online VI NA)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu Online VI NA không đúng dạng (24 hex): {code!r}", "no_code")
        s = new_session()
        r = request(s, "POST", VIEW, json={"search_invoice_id": code}, headers={"Referer": PAGE})
        try:
            res = r.json() or {}
        except ValueError as e:
            raise PdfFetchError(f"Online VI NA trả nội dung lạ (HTTP {r.status_code})", "bad_content") from e
        if res.get("code") == NOT_FOUND:
            raise PdfFetchError(f"Online VI NA không tìm thấy HĐ với mã {code}", "no_pdf")
        data = res.get("data")
        if res.get("code") != OK or not isinstance(data, dict) or not data.get("xml_content"):
            raise PdfFetchError(f"Online VI NA trả lỗi {res.get('code')}: {res.get('message')}", "bad_content")
        m = _SHDON_RE.search(data["xml_content"])
        shdon = str(invoice.get("shdon") or "").strip()
        if not m or (shdon and int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"Mã {code} không phải HĐ số {shdon}", "bad_content")
        return data

    def fetch_xml(self, invoice: dict) -> bytes:
        """TDiep của CQT (bọc HDon có MCCQT + chữ ký) — xml_parser tự tìm HDon bên trong."""
        return self._view(invoice)["xml_content"].encode("utf-8")

    def fetch_pdf(self, invoice: dict) -> bytes:
        from .render import html_to_pdf
        data = self._view(invoice)
        m = _HDON_RE.search(data["xml_content"])
        if not m or not data.get("xslt_content"):
            raise PdfFetchError("Online VI NA không trả HDon / mẫu XSLT để dựng HĐ", "bad_content")
        page = printable(xslt_to_html(m.group(0), data["xslt_content"]))
        pdf = html_to_pdf(page, base=f"{BASE}/")
        if not is_pdf(pdf) or not _pdf_text(pdf):      # PDF trắng thì check_pdf_is_invoice bỏ qua → tự chặn
            raise PdfFetchError("Chromium dựng ra PDF rỗng (mẫu XSLT Online VI NA đổi?)", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link /invoice?id=<mã> hoặc mã 24 hex trong mail. Không mở link."""
        host = (urlsplit(url or "").hostname or "").lower()
        if host not in self.hosts:
            return None
        secret = (link_params(url).get("id") or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"SearchInvoice": secret}}


register(OnlineVinaAdapter())
