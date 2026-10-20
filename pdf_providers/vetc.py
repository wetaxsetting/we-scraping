# pdf_providers/vetc.py
"""
VETC — MSTTCGP 0107500414 (Công ty TNHH Thu phí tự động VETC, tự phát hành HĐ cước
đường bộ, ký hiệu K..TEF) và 0106858609 (Công ty CP VETC, cung cấp HĐ cho VETC TNHH,
vd phí đỗ xe ký hiệu K..TXP). Tra cứu: https://tracuuhoadon.vetc.com.vn/

Không captcha, không cần MST người bán. Khảo sát 10/2026 trên HĐ thật K26TEF-80957898:
  1. GET  /                         → cookie TS01... (WAF F5, có cũng được, không cũng được)
  2. POST /   searchType=SECURE_ID, secureId=<Mã tra cứu> (phân biệt hoa thường)
       → HTML; thấy HĐ: link /download/<mã> và /p/<mã>, số HĐ in đỏ trong bảng kết quả
       → không thấy: "Không tìm thấy hóa đơn, xin vui lòng kiểm tra lại mã tra cứu"
  3. GET  /download/<mã>            → ZIP {ATIS_<số>.xml, .html, .pdf}
       (mã không tồn tại → HTTP 500 JSON "IOError writing file", nên phải tra ở bước 2 trước)

Mã tra cứu: TTKhac "Mã tra cứu" (9-13 ký tự).

⚠ HĐ phí đỗ xe (MSTTCGP 0106858609, K26TXP) KHÔNG có trên cổng này (đã thử 2 HĐ 11 ngày
sau ngày lập, cả tra theo mã vé). VETC chỉ cho xem trong app (Lịch sử → Bãi đỗ). Chưa
tìm được cổng công khai → HĐ của 0106858609 không thấy thì ném 'no_portal' (nằm chờ,
không tốn lượt), còn 0107500414 thì 'no_pdf' (thử lại hằng ngày).
"""
from __future__ import annotations

import io
import zipfile

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

BASE = "https://tracuuhoadon.vetc.com.vn"
MST_TCGP_TNHH = "0107500414"     # tự phát hành, HĐ có trên cổng tra cứu
MST_TCGP_CP = "0106858609"       # HĐ bãi đỗ — chưa có cổng công khai

_NOT_FOUND = "không tìm thấy hóa đơn"


class VetcAdapter(PdfAdapter):
    source_id = "vetc"
    name = "VETC"
    mst_tcgp = (MST_TCGP_TNHH, MST_TCGP_CP)

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return (tt_khac.get("Mã tra cứu") or invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'Mã tra cứu' (VETC)", "no_code")
        s = new_session()
        request(s, "GET", f"{BASE}/")
        r = request(s, "POST", f"{BASE}/",
                    data={"searchType": "SECURE_ID", "secureId": code,
                          "sourceOrg": "", "ticketCode": "", "plate": ""},
                    headers={"Referer": f"{BASE}/", "Origin": BASE})
        if r.status_code != 200:
            raise PdfFetchError(f"Tra cứu VETC trả HTTP {r.status_code}", "other")
        dl = f'href="/download/{code}"'
        if dl not in r.text:
            if _NOT_FOUND in r.text.lower() or 'id="secureIdError2"' in r.text:
                if (invoice.get("mst_tcgp") or "").split("-")[0] == MST_TCGP_CP:
                    raise PdfFetchError(
                        f"HĐ VETC bãi đỗ (MSTTCGP {MST_TCGP_CP}, mã {code}) không có trên "
                        f"tracuuhoadon.vetc.com.vn — chưa biết cổng tra cứu", "no_portal")
                raise PdfFetchError(f"VETC không tìm thấy HĐ (mã {code})", "no_pdf")
            raise PdfFetchError("Trang kết quả VETC không đúng dạng (thiếu link tải)", "bad_content")

        z = request(s, "GET", f"{BASE}/download/{code}", headers={"Referer": f"{BASE}/"})
        if z.status_code != 200:
            raise PdfFetchError(f"Tải ZIP VETC trả HTTP {z.status_code}", "other")
        pdf = self._pdf_from_zip(z.content)
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    @staticmethod
    def _pdf_from_zip(data: bytes) -> bytes:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                name = next((n for n in zf.namelist() if n.lower().endswith(".pdf")), None)
                if name is None:
                    raise PdfFetchError("ZIP của VETC không có file PDF", "no_pdf")
                pdf = zf.read(name)
        except zipfile.BadZipFile as e:
            raise PdfFetchError(f"VETC trả không phải ZIP: {e}", "bad_content") from e
        if not is_pdf(pdf):
            raise PdfFetchError("File PDF trong ZIP của VETC không hợp lệ", "bad_content")
        return pdf


register(VetcAdapter())
