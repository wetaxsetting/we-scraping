"""
Nhóm Mây / Cloudteam — MSTTCGP 0312617990.
Tra cứu: cổng Laravel riêng theo từng khách hàng lớn, dạng <alias>.cloudteam.vn
(đã khảo sát 27/10/2026 trên https://chanel-eportal.cloudteam.vn — cổng của Chanel).

Mỗi khách hàng lớn có subdomain riêng, không suy ra được từ MST người bán → lấy host cổng
từ XML (lookup_url / TTKhac PortalLink). Người bán chưa có cổng → 'no_portal' (nằm chờ).

Quy trình (cùng mã nguồn cho mọi subdomain cloudteam.vn):
  1. GET  /                         → cookie phiên + <meta name="csrf-token">
  2. GET  /refreshcaptcha           → JSON {captcha: "<img src='/captcha/flat?<seed>'>"}
       Ảnh /captcha/flat 120x40 RGBA, 5 ký tự thường+số → model g_120x40_cloud
       (CÙNG cỡ 120x40 với g_120x40_vnpt nên BẮT BUỘC ép đúng nhóm). Server KIỂM captcha:
       sai → JSON {errors:{captcha:[...]}, message:"Vui lòng nhập đúng captcha"}.
  3. POST /check-invoice (JSON {InvNo, captcha}, header X-CSRF-TOKEN)
       → tìm thấy: {linkFile:<url PDF>, linkZip:<url XML>, data:{...}, message, state}
       → không thấy: {message:"Không tìm thấy hóa đơn"} (không có linkFile)
  4. GET  linkFile                  → application/pdf (bản thể hiện, có số HĐ)

Mã tra cứu = "Mã tra cứu hóa đơn" trên cổng = TTKhac/ma_tra_cuu của HĐ.
Trang có nạp recaptcha? KHÔNG — chỉ có captcha ảnh /captcha/flat, server kiểm captcha ảnh này.

Đã kiểm 27/10/2026: captcha g_120x40_cloud server nhận 10/10 (gửi mã bịa, server trả
"Không tìm thấy hóa đơn" = captcha qua; captcha sai trả "Vui lòng nhập đúng captcha").
CHƯA có HĐ thật của Cloudteam trong hệ thống nên luồng tải PDF (bước 4) chưa chạy thật —
PDF luôn qua check_pdf_is_invoice nên sai cũng không lưu nhầm.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request, solve_captcha

CAPTCHA_GROUP = "g_120x40_cloud"
CAPTCHA_TRIES = 4
DEFAULT_BASE = "https://chanel-eportal.cloudteam.vn"   # cổng đã khảo sát (Chanel)

_CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')
_CAPTCHA_SRC_RE = re.compile(r'src="([^"]+)"')


class _WrongCaptcha(Exception):
    pass


class CloudteamAdapter(PdfAdapter):
    source_id = "cloudteam"
    name = "Nhóm Mây (Cloudteam)"
    mst_tcgp = ("0312617990",)
    hosts = ("cloudteam.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail/link Cloudteam: mã tra cứu ở tham số link hoặc `code`. Không mở link."""
        host = (urlsplit(url).hostname or "").lower()
        if not host.endswith("cloudteam.vn"):
            return None
        p = link_params(url)
        secret = (code or p.get("invno") or p.get("key") or p.get("code") or "").strip()
        if not secret:
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"MaTraCuu": secret},
                "lookup_url": f"https://{host}"}

    # ------------------------------------------------------------------
    @staticmethod
    def portal_base(invoice: dict) -> str | None:
        tt_khac = invoice.get("tt_khac") or {}
        link = (tt_khac.get("PortalLink") or invoice.get("lookup_url") or "").strip()
        host = (urlsplit(link).hostname or "").lower()
        if host.endswith("cloudteam.vn"):
            return f"https://{host}"
        return None

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return str(tt_khac.get("MaTraCuu") or tt_khac.get("Matracuu")
                   or invoice.get("ma_tra_cuu") or "").strip()

    # ------------------------------------------------------------------
    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có mã tra cứu Cloudteam", "no_code")
        base = self.portal_base(invoice)
        if not base:
            raise PdfFetchError(
                f"Chưa biết cổng Cloudteam của người bán {invoice.get('nbmst')} — bổ sung "
                f"lookup_url/PortalLink (subdomain <alias>.cloudteam.vn)", "no_portal")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            try:
                link = self._search(base, code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"Cloudteam từ chối captcha {CAPTCHA_TRIES} lần", "captcha")
        pdf_url = urljoin(base + "/", link)
        r = request(new_session(), "GET", pdf_url, headers={"Referer": base + "/"})
        if not is_pdf(r.content):
            raise PdfFetchError(
                f"Cloudteam trả không phải PDF ({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def _search(self, base: str, code: str) -> str:
        s = new_session()                       # captcha gắn cookie phiên
        page = request(s, "GET", base + "/")
        m = _CSRF_RE.search(page.text)
        if not m:
            raise PdfFetchError(f"Không thấy csrf-token trên {base}", "server")
        csrf = m.group(1)
        cap = request(s, "GET", base + "/refreshcaptcha",
                      headers={"Referer": base + "/", "Accept": "application/json"})
        try:
            cap_html = cap.json().get("captcha", "")
        except ValueError as e:
            raise PdfFetchError("Cloudteam refreshcaptcha không trả JSON", "server") from e
        ms = _CAPTCHA_SRC_RE.search(cap_html)
        if not ms:
            raise PdfFetchError("Không tìm thấy ảnh captcha Cloudteam", "server")
        img_url = urljoin(base + "/", ms.group(1).replace("\\/", "/"))
        img = request(s, "GET", img_url, headers={"Referer": base + "/"})
        answer = solve_captcha(img.content, CAPTCHA_GROUP)
        r = request(s, "POST", base + "/check-invoice",
                    data=json.dumps({"InvNo": code, "captcha": answer}),
                    headers={"Referer": base + "/", "Origin": base,
                             "Content-Type": "application/json", "Accept": "application/json",
                             "X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"})
        try:
            data = r.json()
        except ValueError as e:
            raise PdfFetchError(f"Cloudteam check-invoice không trả JSON (HTTP {r.status_code})",
                                "bad_content") from e
        if (data.get("errors") or {}).get("captcha"):
            raise _WrongCaptcha(str(data.get("message")))
        msg = str(data.get("message") or "")
        if "captcha" in msg.lower():
            raise _WrongCaptcha(msg)
        link = data.get("linkFile")
        if not link:
            raise PdfFetchError(f"Cloudteam không tìm thấy HĐ với mã {code} ({msg})", "no_pdf")
        return link


register(CloudteamAdapter())
