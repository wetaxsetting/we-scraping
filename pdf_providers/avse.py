"""
Avsoft-Einvoice (AVSE) — MSTTCGP 0310768095.
Tra cứu: https://hoadondientu.link/tracuutt78  ("MST Người bán" + "Mã tra cứu", không captcha).

ASP.NET MVC 5, form POST thường (không AJAX). Khảo sát 27/10/2026:

    POST /TraCuuTT78/GetInvoice   form maSoThue=<MST bán>, maBaoMat=<mã>
      → 500 trang "Error. An error occurred while processing your request." (274 byte)
        : mã bịa VÀ ô trống đều ra đúng trang này → coi là 'no_pdf' (cổng không có thông báo riêng)
      → thành công: CHƯA THẤY. Adapter nhận PDF trả thẳng, hoặc 1 link PDF cùng host trong trang;
        khác đi thì 'bad_content' để khảo sát lại.
    Cổng còn /TraCuu/GetInvoice (/tracuu) và /Lookup/GetInvoice (/lookup) cho HĐ trước TT78 —
    /Lookup trả 200 thân rỗng với mã bịa. Adapter chỉ dùng cổng TT78.

Mã: TTKhac mã tra cứu / ma_tra_cuu + MST người bán.
Chưa có HĐ thật (DB 27/10: 0 HĐ MSTTCGP 0310768095, 0 link mail) — chỉ kiểm phản hồi "không tìm thấy".
"""
from __future__ import annotations

import requests

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import TIMEOUT, new_session
from .formlookup import lookup_code, pdf_from_response

BASE = "https://hoadondientu.link"
GENERIC_ERROR = "An error occurred while processing your request"


class AvseAdapter(PdfAdapter):
    source_id = "avse"
    name = "Avsoft-Einvoice (hoadondientu.link)"
    mst_tcgp = ("0310768095",)
    hosts = ("hoadondientu.link",)

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có mã tra cứu (AVSE)", "no_code")
        mst = str(invoice.get("nbmst") or "").strip()
        if not mst:
            raise PdfFetchError("Thiếu MST người bán — AVSE cần MST + mã tra cứu", "no_code")
        s = new_session()
        url = f"{BASE}/TraCuuTT78/GetInvoice"
        try:   # gọi thẳng (không qua common.request): cổng trả 500 cho cả "không tìm thấy"
            r = s.request("POST", url, data={"maSoThue": mst, "maBaoMat": code}, timeout=TIMEOUT,
                          headers={"Referer": f"{BASE}/tracuutt78"})
        except requests.Timeout as e:
            raise PdfFetchError(f"Timeout {url}: {e}", "network") from e
        except requests.RequestException as e:
            raise PdfFetchError(f"Lỗi kết nối {url}: {e}", "network") from e
        if r.status_code == 500 and GENERIC_ERROR in (r.text or ""):
            raise PdfFetchError(f"AVSE không tìm thấy HĐ (MST {mst}, mã {code}) — cổng trả trang lỗi 500",
                                "no_pdf")
        if r.status_code >= 500:
            raise PdfFetchError(f"HTTP {r.status_code} từ {url}", "server")
        if r.status_code != 200:
            raise PdfFetchError(f"AVSE tra cứu trả HTTP {r.status_code}", "other")
        if not r.content:
            raise PdfFetchError(f"AVSE không tìm thấy HĐ (MST {mst}, mã {code}) — thân rỗng", "no_pdf")
        return pdf_from_response(s, r, invoice, self.name)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Cần cả MST người bán — mail chưa có mẫu nên chưa nhận link. Không mở link."""
        return None


register(AvseAdapter())
