"""
Mắt Bão (Matbao-invoice / MIFI) — MSTTCGP 0302712571. Tra cứu: https://matbao.in/tra-cuu-hoa-don
(Next.js; API của chính trang dưới /api/tra-cuu-hoa-don, gọi tiếp sang portal-group1.mifi.vn).

Mã tra cứu = "Fkey" (10 ký tự, vd 2EF6276D0B): XML Thuế KHÔNG có (không TTKhac nào), chỉ in trên
PDF ("nhập mã: ...") và trong mail. Nhưng cổng có chức năng "Tra cứu dùng file XML": từ 4 trường
của XML hỏi ra Fkey → tra được thẳng từ dữ liệu Thuế, không cần mail.

Quy trình (khảo sát + kiểm trên HĐ thật 26/10/2026):
  1. (khi chưa có mã) GET /api/tra-cuu-hoa-don/fkey?taxcode=<MST bán>&pattern=<KHMSHDon>&serial=<KHHDon>&no=<SHDon>
       → {"fkey": "2EF6276D0B"} | {"fkey": null}       (số HĐ có/không số 0 đầu đều được)
  2. GET /api/tra-cuu-hoa-don?code=<Fkey>&keyword=
       → {"found": true, "symbol": "1C24TAB", "number": "1282", "viewerUrl", "pdfUrl", "xmlUrl", "checkUrl"}
       → {"found": false}
  3. GET pdfUrl (portal-group1.mifi.vn/downloadpdfca.aspx?...) → application/pdf
     GET xmlUrl (…/downloadxml.aspx?...)                       → XML có chữ ký
Trang hiện ô reCAPTCHA v2 và JS gửi kèm `token`, nhưng server KHÔNG kiểm: gửi không token / token
rỗng đều trả kết quả (kiểm 26/10). Client tự hiểu HTTP 403 là "captcha sai" → gặp 403 tức server
đã bật kiểm reCAPTCHA thật: dừng (no_portal), KHÔNG vượt.
PDF tải về là bản dựng lại theo mẫu hiện hành: cùng nội dung PDF trong mail, khác vài chi tiết trình
bày (cột thuế "KCT 0" thay "-", dòng chân trang ghi matbao.in thay mifi.vn).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://matbao.in"
PAGE = BASE + "/tra-cuu-hoa-don"
API = BASE + "/api/tra-cuu-hoa-don"
_CODE_RE = re.compile(r"^[A-Za-z0-9]{5,64}$")          # như trang (regex D trong bundle)


class MatBaoAdapter(PdfAdapter):
    source_id = "matbao"
    name = "Mắt Bão (matbao.in)"
    mst_tcgp = ("0302712571",)
    hosts = ("matbao.in",)                # gồm portal.matbao.in, <MST>.matbao.in

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for v in (invoice.get("ma_tra_cuu"), tt_khac.get("Fkey"), tt_khac.get("Mã tra cứu")):
            if v and str(v).strip():
                return str(v).strip()
        return ""

    # -- tra ------------------------------------------------------------------

    def _fkey(self, s, invoice: dict) -> str:
        """Fkey từ (MST bán, mẫu, ký hiệu, số) — chức năng "Tra cứu dùng file XML" của cổng."""
        params = {"taxcode": str(invoice.get("nbmst") or "").strip(),
                  "pattern": str(invoice.get("khmshdon") or "").strip(),
                  "serial": str(invoice.get("khhdon") or "").strip(),
                  "no": str(invoice.get("shdon") or "").strip()}
        if not all(params.values()):
            raise PdfFetchError("Không có mã tra cứu Mắt Bão và thiếu MST/mẫu/ký hiệu/số để hỏi Fkey", "no_code")
        r = request(s, "GET", API + "/fkey", params=params, headers={"Referer": PAGE})
        self._check_status(r)
        try:
            fkey = (r.json() or {}).get("fkey")
        except ValueError as e:
            raise PdfFetchError(f"Mắt Bão /fkey trả nội dung lạ (HTTP {r.status_code})", "bad_content") from e
        if not fkey:
            raise PdfFetchError(
                f"Mắt Bão không tìm thấy HĐ {params['pattern']}{params['serial']}-{params['no']} "
                f"của {params['taxcode']}", "no_pdf")
        return str(fkey).strip()

    def _lookup(self, invoice: dict):
        """→ (session, kết quả JSON đã kiểm đúng số HĐ)."""
        s = new_session()
        code = self.lookup_code(invoice) or self._fkey(s, invoice)
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu Mắt Bão không đúng dạng: {code!r}", "no_code")
        r = request(s, "GET", API, params={"code": code, "keyword": ""}, headers={"Referer": PAGE})
        self._check_status(r)
        try:
            res = r.json() or {}
        except ValueError as e:
            raise PdfFetchError(f"Mắt Bão tra cứu trả nội dung lạ (HTTP {r.status_code})", "bad_content") from e
        if not res.get("found"):
            raise PdfFetchError(f"Mắt Bão không tìm thấy HĐ với mã {code}", "no_pdf")
        shdon, number = str(invoice.get("shdon") or "").strip(), str(res.get("number") or "").strip()
        if shdon and number.isdigit() and int(number) != int(shdon):
            raise PdfFetchError(f"Mã {code} là HĐ số {number}, không phải {shdon}", "bad_content")
        return s, res

    @staticmethod
    def _check_status(r) -> None:
        if r.status_code == 403:
            raise PdfFetchError("Mắt Bão trả 403 — server đã bật kiểm reCAPTCHA, không vượt", "no_portal")
        if r.status_code != 200:
            raise PdfFetchError(f"Mắt Bão trả HTTP {r.status_code}", "bad_content")

    # -- tải ------------------------------------------------------------------

    def fetch_pdf(self, invoice: dict) -> bytes:
        s, res = self._lookup(invoice)
        if not res.get("pdfUrl"):
            raise PdfFetchError("Mắt Bão tìm thấy HĐ nhưng không có link PDF (có thể cần mã khách hàng)", "no_pdf")
        r = request(s, "GET", res["pdfUrl"], headers={"Referer": PAGE})
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(
                f"Mắt Bão trả nội dung không phải PDF ({r.headers.get('Content-Type')}, HTTP {r.status_code})",
                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        s, res = self._lookup(invoice)
        if not res.get("xmlUrl"):
            raise PdfFetchError("Mắt Bão không có link XML cho HĐ này", "no_pdf")
        r = request(s, "GET", res["xmlUrl"], headers={"Referer": PAGE})
        head = (r.content or b"").lstrip()[:200].lower()
        if r.status_code != 200 or not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError(f"Mắt Bão trả nội dung không phải XML (HTTP {r.status_code})", "bad_content")
        return r.content

    # -- link trong mail -------------------------------------------------------

    def invoice_from_link(self, url: str, code, mail_text: str):
        """
        Link trong mail Mắt Bão (matbao.in, portal.matbao.in/Invoice/ViewFromEmail?token=...,
        .../HoaDonDauVao/KiemTraHopLe/...) không tự chứa Fkey dạng đọc được → dùng mã đã bóc trong
        mail; trang tra cứu thì nhận cả ?ma-tra-cuu=<mã>. Không mở link.
        """
        host = (urlsplit(url or "").hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        secret = (link_params(url).get("ma-tra-cuu") or code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Fkey": secret}}


register(MatBaoAdapter())
