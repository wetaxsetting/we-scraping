"""
247Express — CÔNG TY CỔ PHẦN HAI BỐN BẢY (người bán MST 0304043037), cổng riêng
https://hoadon.247express.vn (SPA React + API ASP.NET dưới /service/api/).

HĐ của 247Express là hthdon=2 (ký hiệu 1K..THC, gửi Thuế theo bảng tổng hợp) → Thuế KHÔNG có XML,
nên không có MSTTCGP/TTKhac để định tuyến. Mã tra cứu chỉ có trong mail
"Hóa đơn điện tử số <số> - CÔNG TY CỔ PHẦN HAI BỐN BẢY": 24 ký tự HOA + số (vd OWJQPNIJU4GAAUEMGH53RL1X),
kèm 3 link: trang chủ, /tra-cuu/<mã>, /service/api/Invoice/DownloadFilePDF/<mã>.
→ Adapter chỉ dùng qua luồng link trong mail (process_links), PDF khớp HĐ theo chữ trong PDF
(MST bán + ký hiệu + số).

Quy trình (khảo sát 23/10/2026):
  GET /service/api/Invoice/DownloadFilePDF/<mã>  → 200 application/pdf (bản thể hiện, có "Mã tra cứu: <mã>")
                                                 → mã sai: 404 application/json
Không captcha cho endpoint tải PDF trực tiếp (chính là link "Tải hóa đơn" NCC gửi trong mail).
Form tra cứu trên trang web (nhập mã tay, xem HTML, tải XML) dùng reCAPTCHA → KHÔNG dùng, nên
adapter không lấy được XML.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

BASE = "https://hoadon.247express.vn"
PDF_URL = BASE + "/service/api/Invoice/DownloadFilePDF/{code}"

_CODE_RE = re.compile(r"^[A-Z0-9]{16,40}$")
# Đường dẫn trong link mail chứa mã: /tra-cuu/<mã>, /service/api/Invoice/DownloadFilePDF/<mã>
_PATH_CODE_RE = re.compile(r"/(?:tra-cuu|DownloadFilePDF)/([A-Za-z0-9]{16,40})/?$", re.I)


class Express247Adapter(PdfAdapter):
    source_id = "express247"
    name = "247Express (hoadon.247express.vn)"
    mst_tcgp = ()                     # hthdon=2: không có XML → không có MSTTCGP để định tuyến
    hosts = ("247express.vn",)        # gồm cả hoadon.247express.vn

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mã lấy từ đường dẫn link, không thì từ mã đã bóc trong mail. Không mở link."""
        m = _PATH_CODE_RE.search(urlsplit(url).path or "")
        secret = (m.group(1) if m else (code or "")).strip().upper()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(invoice.get("ma_tra_cuu") or tt_khac.get("Mã tra cứu") or "").strip().upper()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("Không có mã tra cứu 247Express (chỉ có trong mail của NCC)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu 247Express không đúng dạng: {code!r}", "no_code")
        s = new_session()
        r = request(s, "GET", PDF_URL.format(code=code), headers={"Referer": BASE + "/"})
        if r.status_code == 404:
            raise PdfFetchError(f"247Express không tìm thấy HĐ với mã {code}", "no_pdf")
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(
                f"247Express trả nội dung không phải PDF ({r.headers.get('Content-Type')}, HTTP {r.status_code})",
                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content


register(Express247Adapter())
