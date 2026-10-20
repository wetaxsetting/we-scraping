# pdf_providers/misa.py
"""
MISA meInvoice — MSTTCGP 0101243150. Tra cứu: https://www.meinvoice.vn/tra-cuu/

Không cần captcha, cookie hay tìm kiếm trước: 1 lệnh GET theo TransactionID trả
thẳng PDF. Bắt buộc header Accept kiểu trình duyệt (có text/html) — thiếu nó server
vẫn trả 200 nhưng body rỗng, rất dễ tưởng là "không tìm thấy".

    GET /tra-cuu/DownloadHandler.ashx?Type=pdf&Code=<TransactionID>
      → 200 application/pdf + PDF          : thành công
      → 200 application/pdf + 0 byte       : mã không tồn tại / HĐ chưa sẵn sàng

Mã tra cứu: TTKhac "TransactionID" (cũng là thuộc tính Id của DLHDon và 12 ký tự
cuối của GUID trong QR trường 99-00). Không cần MST người bán.

Khảo sát 10/2026 trên HĐ thật C26TCP-6129. Site có captcha ẩn (ImageHandler.ashx,
115x30) chỉ bật cho API tìm kiếm sau nhiều lần tìm sai — đường tải PDF không bị chặn.
"""
from __future__ import annotations

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://www.meinvoice.vn/tra-cuu"
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class MisaAdapter(PdfAdapter):
    source_id = "misa_meinvoice"
    name = "MISA meInvoice"
    mst_tcgp = ("0101243150",)
    hosts = ("meinvoice.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail MISA: .../tra-cuu/?sc=<TransactionID> hoặc ...EmailTrackingHandler.ashx?TransactionID=..."""
        p = link_params(url)
        tid = p.get("sc") or p.get("transactionid")
        return {"tt_khac": {"TransactionID": tid}, "ma_tra_cuu": tid} if tid else None

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return (tt_khac.get("TransactionID") or invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có TransactionID (mã tra cứu MISA)", "no_code")
        s = new_session()
        r = request(s, "GET", f"{BASE}/DownloadHandler.ashx",
                    params={"Type": "pdf", "Code": code},
                    headers={"Accept": ACCEPT_HTML, "Referer": f"{BASE}/"})
        if r.status_code != 200:
            raise PdfFetchError(f"MISA trả HTTP {r.status_code}", "other")
        if not r.content:
            raise PdfFetchError(f"MISA chưa có PDF cho mã {code} (không tồn tại / chưa cấp mã)",
                                "no_pdf")
        if not is_pdf(r.content):
            raise PdfFetchError(
                f"MISA trả nội dung không phải PDF ({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content


register(MisaAdapter())
