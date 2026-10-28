# pdf_providers/easyinvoice.py
"""
EasyInvoice (Softdreams) — MSTTCGP 0105987432.

Hai cổng tra cứu, cùng một quy trình:
  * chung:     https://tracuu.easyinvoice.vn        — cần Fkey + MST người bán (GIỮ dấu '-')
  * người bán: PortalLink trong TTKhac, vd http://0104947250001hd.easyinvoice.com.vn — chỉ cần Fkey

Quy trình (khảo sát 10/2026 trên HĐ thật C26TPS-4538):
  1. GET  /Search/Index
  2. GET  /Captcha/Show            → PNG 150x34, gắn với cookie ASP.NET_SessionId → model g_150x34
  3. POST /Search/Search           typeSearch="", FKey, Capcha(*), [TaxCode]   (*) đúng chính tả "Capcha"
       → HTML: #msg (lỗi), #InvData (JSON, khóa "str" = HTML hóa đơn), token trong showInv(...)
  4. POST /Invoice/DownloadPdfAndFileAttachFromAvailableHtml   token, html=base64(InvData.str)
       → {"fileGuid", "fileName"}
  5. GET  /Invoice/Download?fileGuid&fileName → ZIP chứa 1 PDF

Lưu ý: PDF do server dựng (Chromium) từ HTML ta gửi lên ở bước 4 — gửi html rỗng vẫn ra
PDF trắng, nên PDF luôn được kiểm có chứa số hóa đơn. Bản PDF lưu sẵn chỉ tải được khi
đăng nhập. HĐ nhiều trang (vượt RowPerPage) chưa kiểm thử: ngắt trang có thể khác bản
xem trên cổng.
"""
from __future__ import annotations

import base64
import html
import io
import json
import re
import zipfile

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request, solve_captcha

GENERIC_BASE = "https://tracuu.easyinvoice.vn"
CAPTCHA_GROUP = "g_150x34"
CAPTCHA_TRIES = 3

_MSG_RE = re.compile(r'<input[^>]*id="msg"[^>]*value="([^"]*)"')
_INV_RE = re.compile(r'<input[^>]*id="InvData"[^>]*value="([^"]*)"')
_TOKEN_RE = re.compile(r"showInv\(data\.str,.*?,\s*'([^']*)'\);", re.S)


class _WrongCaptcha(Exception):
    pass


class _TryOtherPortal(Exception):
    pass


class EasyInvoiceAdapter(PdfAdapter):
    source_id = "easyinvoice"
    name = "EasyInvoice (Softdreams)"
    mst_tcgp = ("0105987432",)
    hosts = ("easyinvoice.vn", "easyinvoice.com.vn")

    def invoice_from_link(self, url: str, code, mail_text: str):
        """
        Mail EasyInvoice: http://<MST>hd.easyinvoice.vn/Invoice/DownloadInvPdf?token=<base64>,
        token = "<mẫu><ký hiệu>__<số>|<Fkey>". Fkey có 2 kiểu:
          * cổng chung/nhiều người bán: thuần chữ-số, vd 1C26TAL__37|RZAPJM7MQ
          * một số cổng riêng (vd 8404421833-001, 8530810438-002): Fkey chứa dấu '-',
            vd 2C24MAA__342|M2-24-DA8RU-17161791187  → phải cho phép '-' trong Fkey, nếu
            không regex cũ cắt mất đuôi (chỉ lấy "M2") và tra cứu sai.
        Chỉ giải mã chuỗi, không mở link.
        """
        from urllib.parse import urlsplit
        token = link_params(url).get("token")
        host = (urlsplit(url).hostname or "").lower()
        m_host = re.match(r"(\d{10})(\d{3})?hd\.", host)
        if not token or not m_host:
            return None
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None
        m = re.match(r"(\d)([A-Z0-9]{6})__(\d+)\|([A-Za-z0-9][A-Za-z0-9\-]*)", raw)
        if not m:
            return None
        nbmst = m_host.group(1) + (f"-{m_host.group(2)}" if m_host.group(2) else "")
        return {"nbmst": nbmst, "khmshdon": int(m.group(1)), "khhdon": m.group(2), "shdon": int(m.group(3)),
                "ma_tra_cuu": m.group(4), "tt_khac": {"Fkey": m.group(4)}, "lookup_url": f"http://{host}"}

    # ------------------------------------------------------------------
    @staticmethod
    def portals(invoice: dict) -> list:
        """[(base, cần TaxCode?)]: cổng chung trước, rồi cổng riêng của người bán."""
        tt_khac = invoice.get("tt_khac") or {}
        out = [(GENERIC_BASE, True)]
        seller = (tt_khac.get("PortalLink") or invoice.get("lookup_url") or "").strip().rstrip("/")
        if not seller and invoice.get("nbmst"):
            seller = f"http://{invoice['nbmst'].replace('-', '')}hd.easyinvoice.com.vn"
        if seller and "easyinvoice" in seller:
            out.append((seller, False))
        return out

    @staticmethod
    def fkey(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return (tt_khac.get("Fkey") or tt_khac.get("FKey") or invoice.get("ma_tra_cuu") or "").strip()

    # ------------------------------------------------------------------
    def fetch_pdf(self, invoice: dict) -> bytes:
        fkey = self.fkey(invoice)
        if not fkey:
            raise PdfFetchError("XML không có Fkey (mã tra cứu EasyInvoice)", "no_code")
        last_not_found = None
        for base, needs_tax in self.portals(invoice):
            try:
                return self._fetch_from(base, fkey, invoice.get("nbmst", "") if needs_tax else None,
                                        invoice)
            except _TryOtherPortal as e:
                last_not_found = str(e)
        raise PdfFetchError(f"EasyInvoice không tìm thấy HĐ (Fkey {fkey}): {last_not_found}", "no_pdf")

    def _fetch_from(self, base: str, fkey: str, tax_code, invoice: dict) -> bytes:
        for attempt in range(1, CAPTCHA_TRIES + 1):
            s = new_session()          # captcha gắn với cookie phiên → mỗi lần thử 1 phiên mới
            try:
                inv_html, token = self._search(s, base, fkey, tax_code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"EasyInvoice từ chối captcha {CAPTCHA_TRIES} lần", "captcha")
        return self._download(s, base, inv_html, token, invoice)

    def _search(self, s, base: str, fkey: str, tax_code):
        index = f"{base}/Search/Index"
        request(s, "GET", index)
        img = request(s, "GET", f"{base}/Captcha/Show", headers={"Referer": index})
        if img.status_code != 200 or not img.content:
            raise PdfFetchError(f"Không lấy được captcha từ {base}", "server")
        data = {"typeSearch": "", "FKey": fkey, "Capcha": solve_captcha(img.content, CAPTCHA_GROUP)}
        if tax_code is not None:
            data["TaxCode"] = tax_code
        r = request(s, "POST", f"{base}/Search/Search", data=data,
                    headers={"Referer": index, "Origin": base})
        if r.status_code != 200:
            raise PdfFetchError(f"Tra cứu EasyInvoice trả HTTP {r.status_code}", "other")
        page = r.text
        msg = html.unescape(m.group(1)) if (m := _MSG_RE.search(page)) else ""
        if "capcha" in msg.lower() or "captcha" in msg.lower():
            raise _WrongCaptcha(msg)
        if "không tồn tại" in msg.lower():
            raise _TryOtherPortal(msg)
        if msg:
            raise PdfFetchError(f"EasyInvoice: {msg}", "other")
        raw = html.unescape(m.group(1)) if (m := _INV_RE.search(page)) else ""
        token = m.group(1) if (m := _TOKEN_RE.search(page)) else ""
        if not raw or not token:
            raise _TryOtherPortal("Trang kết quả không có dữ liệu hóa đơn")
        try:
            inv_html = json.loads(raw)["str"]
        except (ValueError, KeyError, TypeError) as e:
            raise PdfFetchError(f"InvData không đọc được: {e}", "bad_content") from e
        return inv_html, token

    def _download(self, s, base: str, inv_html: str, token: str, invoice: dict) -> bytes:
        r = request(s, "POST", f"{base}/Invoice/DownloadPdfAndFileAttachFromAvailableHtml",
                    data={"token": token,
                          "html": base64.b64encode(inv_html.encode("utf-8")).decode("ascii")},
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{base}/Search/Index"})
        try:
            meta = r.json()
        except ValueError as e:
            raise PdfFetchError(f"EasyInvoice trả không phải JSON khi tạo PDF (HTTP {r.status_code})",
                                "bad_content") from e
        if meta.get("msg") or not meta.get("fileGuid"):
            raise PdfFetchError(f"EasyInvoice không tạo được PDF: {meta.get('msg') or meta}", "no_pdf")
        z = request(s, "GET", f"{base}/Invoice/Download",
                    params={"fileGuid": meta["fileGuid"], "fileName": meta.get("fileName", "")})
        try:
            with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
                name = next((n for n in zf.namelist() if n.lower().endswith(".pdf")), None)
                if name is None:
                    raise PdfFetchError("ZIP của EasyInvoice không có file PDF", "no_pdf")
                pdf = zf.read(name)
        except zipfile.BadZipFile as e:
            raise PdfFetchError(f"EasyInvoice trả ZIP hỏng: {e}", "bad_content") from e
        if not is_pdf(pdf):
            raise PdfFetchError("File trong ZIP không phải PDF hợp lệ", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf


register(EasyInvoiceAdapter())
