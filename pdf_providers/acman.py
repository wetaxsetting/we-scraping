"""
ACMan (CTCP ACMan) — MSTTCGP 0104908371.
Tra cứu: https://hoadondientu.acman.vn/tra-cuu/hoa-don.html  (không captcha).

ASP.NET WebForms, 2 ô: "Mã số thuế đơn vị phát hành" (txtMaDonViPhatHanh = MST người bán) và
"Mã tra cứu hóa đơn (MST đơn vị nhận)" (txtMaTraCuuHoaDon). Khảo sát 27/10/2026:

    GET  /tra-cuu/hoa-don.html → lấy __VIEWSTATE / __EVENTVALIDATION
    POST /tra-cuu/hoa-don.html  __EVENTTARGET=ctl00$PageContent$btnTraCuu + 2 ô trên
      → 200 HTML "Không tìm thấy hóa đơn, vui lòng kiểm tra thông tin và thử lại"  : → 'no_pdf'
        (gửi MST ACMan + MST người mua 1201496252 → không tìm thấy)
      → thành công: CHƯA THẤY. Adapter nhận PDF trả thẳng, hoặc 1 link PDF cùng host trong trang;
        khác đi thì 'bad_content' để khảo sát lại.

Mã: TTKhac mã tra cứu / ma_tra_cuu. Nhãn ô ghi "(MST đơn vị nhận)" nhưng dict HĐ không có MST người
mua nên không thử MST người mua.
Chưa có HĐ thật (DB 27/10: 0 HĐ MSTTCGP 0104908371, 0 link mail) — chỉ kiểm phản hồi "không tìm thấy".
"""
from __future__ import annotations

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import new_session, request
from .formlookup import hidden_fields, lookup_code, pdf_from_response

URL = "https://hoadondientu.acman.vn/tra-cuu/hoa-don.html"
NOT_FOUND = "Không tìm thấy hóa đơn"


class AcmanAdapter(PdfAdapter):
    source_id = "acman"
    name = "ACMan (hoadondientu.acman.vn)"
    mst_tcgp = ("0104908371",)
    hosts = ("hoadondientu.acman.vn",)

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có mã tra cứu (ACMan)", "no_code")
        mst = str(invoice.get("nbmst") or "").strip()
        if not mst:
            raise PdfFetchError("Thiếu MST người bán — ACMan cần MST + mã tra cứu", "no_code")
        s = new_session()
        r = request(s, "GET", URL)
        if r.status_code != 200:
            raise PdfFetchError(f"ACMan trả HTTP {r.status_code}", "other")
        form = hidden_fields(r.text)
        if "__VIEWSTATE" not in form:
            raise PdfFetchError("ACMan: trang tra cứu không có __VIEWSTATE (đổi giao diện?)", "bad_content")
        form.update({"__EVENTTARGET": "ctl00$PageContent$btnTraCuu", "__EVENTARGUMENT": "",
                     "ctl00$PageContent$txtMaDonViPhatHanh": mst,
                     "ctl00$PageContent$txtMaTraCuuHoaDon": code})
        r = request(s, "POST", URL, data=form, headers={"Referer": URL})
        if r.status_code != 200:
            raise PdfFetchError(f"ACMan tra cứu trả HTTP {r.status_code}", "other")
        if NOT_FOUND in (r.text or ""):
            raise PdfFetchError(f"ACMan không tìm thấy HĐ (MST {mst}, mã {code})", "no_pdf")
        return pdf_from_response(s, r, invoice, self.name)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Cổng cần cả MST người bán, chưa có mẫu mail ACMan → chưa nhận link. Không mở link."""
        return None


register(AcmanAdapter())
