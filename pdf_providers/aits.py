# pdf_providers/aits.py
"""
AITS — Công ty TNHH Tin học - Viễn thông Hàng không, MSTTCGP 0103019524. Tra cứu: https://einvoice.aits.vn/

Form tra cứu không captcha (captcha Telerik trên trang thuộc form liên hệ khác), GET mở tab mới:
    GET /api/Tracuu/E-Invoice?mst=<MST người bán>&id=<Số bảo mật>
        → không tìm thấy: 200 text/html "Value cannot be null.\r\nParameter name: content" (46 byte)
        → thấy HĐ: server trả file (thông báo trên = HttpContent rỗng). Chưa thấy dạng thật: nhận PDF,
          hoặc ZIP có PDF (form "Xem hóa đơn" kế bên nhận file *.zip của HĐ AITS).
Cần MST người bán (nbmst) + "Số bảo mật" (TTKhac / ma_tra_cuu).

Khảo sát 27/10/2026: chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy.
"""
from __future__ import annotations

import io
import re
import zipfile

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://einvoice.aits.vn"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_MST_RE = re.compile(r"^\d{10}(-\d{3})?$")
_CODE_KEYS = ("Số bảo mật", "Mã số bảo mật", "Mã bảo mật", "SoBaoMat", "Mã tra cứu", "Mã tra cứu hóa đơn")


class AitsAdapter(PdfAdapter):
    source_id = "aits"
    name = "AITS (einvoice.aits.vn)"
    mst_tcgp = ("0103019524",)
    hosts = ("einvoice.aits.vn",)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for c in [tt_khac.get(k) for k in _CODE_KEYS] + [invoice.get("ma_tra_cuu")]:
            c = str(c or "").strip()
            if _CODE_RE.match(c):
                return c
        return ""

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        mst = str(invoice.get("nbmst") or "").strip()
        if not code:
            raise PdfFetchError("XML không có 'Số bảo mật' (AITS)", "no_code")
        if not _MST_RE.match(mst):
            raise PdfFetchError("Thiếu MST người bán để tra AITS", "no_code")
        s = new_session()
        r = request(s, "GET", f"{BASE}/api/Tracuu/E-Invoice", params={"mst": mst, "id": code},
                    headers={"Referer": f"{BASE}/"})
        if r.status_code == 404 or (r.status_code == 200 and b"Value cannot be null" in (r.content or b"")[:200]):
            raise PdfFetchError(f"AITS không tìm thấy HĐ (MST {mst}, số bảo mật {code})", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"Tra cứu AITS trả HTTP {r.status_code}", "other")
        pdf = self._pdf_from(r.content or b"")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    @staticmethod
    def _pdf_from(data: bytes) -> bytes:
        if is_pdf(data):
            return data
        if data[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    name = next((n for n in zf.namelist() if n.lower().endswith(".pdf")), None)
                    if name is None:
                        raise PdfFetchError("ZIP của AITS không có file PDF", "no_pdf")
                    pdf = zf.read(name)
            except zipfile.BadZipFile as e:
                raise PdfFetchError(f"AITS trả ZIP hỏng: {e}", "bad_content") from e
            if is_pdf(pdf):
                return pdf
        raise PdfFetchError("AITS trả nội dung không phải PDF/ZIP có PDF", "bad_content")

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Chỉ dùng được link có sẵn `mst` + `id` (link API tra cứu). Không mở link."""
        p = link_params(url)
        mst, sid = p.get("mst", ""), p.get("id") or (code or "").strip()
        if not (_MST_RE.match(mst) and _CODE_RE.match(sid)):
            return None
        return {"nbmst": mst, "ma_tra_cuu": sid, "tt_khac": {"Số bảo mật": sid}}


register(AitsAdapter())
