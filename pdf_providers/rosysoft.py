"""
ROSY (Công ty TNHH Rosy Soft, phần mềm ROSY_ERP) — MSTTCGP 0305142231.
Tra cứu: https://einv.rosysoft.vn:8386  (ASP.NET MVC + Kendo grid; 1 cổng chung cho mọi người bán,
phân biệt bằng MST người bán). Người bán đã gặp: Ánh Dương Việt Nam / Vinasun 0302035520
(HĐ taxi tháng, ký hiệu K..TAN, gửi mail hằng tháng).

Không captcha (trang /Account/LogIn chỉ có CSS tên recaptcha-anchor-label, không widget/sitekey).
Quy trình (khảo sát + kiểm trên HĐ thật 26/10/2026):
  1. GET  /RSeInvoiceSearch?TaxCode=<MST bán>&InvID=<mã tra cứu>              (tab "Mã tra cứu")
     hoặc GET /RSeInvoiceSearch?TaxCode=<MST bán>&InvSerial=<mẫu+ký hiệu>&InvNo=<số>   (tab "Số HĐ")
       → server ghi điều kiện tra vào phiên (cookie ASP.NET_SessionId)
       InvSerial phải gồm cả số mẫu (vd "1K24TAN", không phải "K24TAN"); InvNo không có số 0 đầu.
       Ô "Tổng tiền" của form là tùy chọn — không gửi thì vẫn ra; gửi sai (kể cả 0) thì không ra.
  2. POST /RSeInvoiceSearch/Read (X-Requested-With) → {"Data":[{InvID, InvSerial, InvNo, TaxCode,
       CusTaxCode, Total, PINCode, ...}]}. Không thấy → Data rỗng.
  3. POST /RSeInvoiceDetail/CallApi_GetPDF_HDDT {id, taxcode, PINcode} → {"success", "PDF_File": base64}
     POST /RSeInvoiceDetail/viewXml {id, taxcode, PINcode} → XML gốc có chữ ký người bán + MCCQT.
Mail của Rosy: link /RSeInvoiceSearch/index?TaxCode=<MST bán>&InvID=<mã> (+ /RSeInvoiceDetail/ViewHtml
?id=&taxcode=&PINcode=) — đọc tham số trên link, không mở link. Tiêu đề mail chỉ có ký hiệu
("1K24TAN 1 | E-Invoice ...") không có số HĐ → khớp HĐ bằng chữ trong PDF / XML tải về.
"""
from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://einv.rosysoft.vn:8386"
_AJAX = {"X-Requested-With": "XMLHttpRequest"}
_GRID = {"sort": "", "page": "1", "pageSize": "100", "group": "", "filter": ""}
_INVID_RE = re.compile(r"^[A-Za-z0-9]{8,40}$")
_MST_RE = re.compile(r"^\d{10}(?:-\d{3})?$")


def _tt(invoice: dict, *names) -> str:
    tt_khac = invoice.get("tt_khac") or {}
    for n in names:
        v = str(tt_khac.get(n) or "").strip()
        if v:
            return v
    return ""


class RosySoftAdapter(PdfAdapter):
    source_id = "rosysoft"
    name = "ROSY e-Invoice (einv.rosysoft.vn)"
    mst_tcgp = ("0305142231",)
    hosts = ("rosysoft.vn",)                 # gồm einv.rosysoft.vn

    # -- tra cứu -------------------------------------------------------------

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        """InvID (mã tra cứu Rosy, vd A01TXL000665500) — TTKhac nếu có, rồi ma_tra_cuu."""
        return _tt(invoice, "InvID", "Mã tra cứu") or str(invoice.get("ma_tra_cuu") or "").strip()

    def _search(self, invoice: dict):
        """Trả (session, dòng grid khớp HĐ)."""
        mst = str(invoice.get("nbmst") or "").strip()
        if not mst:
            raise PdfFetchError("Thiếu MST người bán để tra Rosy", "no_code")
        code = self.lookup_code(invoice)
        shdon = str(invoice.get("shdon") or "").strip()
        khhdon = str(invoice.get("khhdon") or "").strip()
        if code and _INVID_RE.match(code):
            params = {"TaxCode": mst, "InvID": code}
        elif shdon and khhdon:
            params = {"TaxCode": mst, "InvSerial": f"{invoice.get('khmshdon') or 1}{khhdon}",
                      "InvNo": str(int(shdon))}
        else:
            raise PdfFetchError("Không có mã tra cứu (InvID) hay ký hiệu + số HĐ để tra Rosy", "no_code")
        s = new_session()
        request(s, "GET", f"{BASE}/RSeInvoiceSearch", params=params)
        r = request(s, "POST", f"{BASE}/RSeInvoiceSearch/Read", data=_GRID,
                    headers={**_AJAX, "Referer": f"{BASE}/RSeInvoiceSearch"}, allow_redirects=False)
        try:
            rows = r.json().get("Data") or []
        except ValueError as e:
            raise PdfFetchError(f"Rosy trả HTTP {r.status_code}, không phải JSON grid", "bad_content") from e
        if shdon:
            rows = [x for x in rows if str(x.get("InvNo") or "").lstrip("0") == str(int(shdon))]
        if not rows:
            raise PdfFetchError(f"Rosy không tìm thấy HĐ ({', '.join(f'{k}={v}' for k, v in params.items())})",
                                "no_pdf")
        if len(rows) > 1:
            raise PdfFetchError(f"Rosy trả {len(rows)} HĐ, không xác định được HĐ cần lấy", "no_pdf")
        return s, rows[0]

    @staticmethod
    def _key(row: dict) -> dict:
        return {"id": row.get("InvID") or "", "taxcode": row.get("TaxCode") or "",
                "PINcode": row.get("PINCode") or ""}

    # -- tải -------------------------------------------------------------------

    def fetch_pdf(self, invoice: dict) -> bytes:
        s, row = self._search(invoice)
        r = request(s, "POST", f"{BASE}/RSeInvoiceDetail/CallApi_GetPDF_HDDT", data=self._key(row),
                    headers=_AJAX)
        try:
            j = r.json()
        except ValueError as e:
            raise PdfFetchError(f"Rosy trả HTTP {r.status_code} khi lấy PDF, không phải JSON", "bad_content") from e
        if not j.get("success") or not j.get("PDF_File"):
            raise PdfFetchError(f"Rosy không trả PDF: {j.get('message') or 'không có thông báo'}", "no_pdf")
        try:
            pdf = base64.b64decode(j["PDF_File"])
        except (binascii.Error, ValueError) as e:
            raise PdfFetchError("PDF_File của Rosy không phải base64", "bad_content") from e
        if not is_pdf(pdf):
            raise PdfFetchError("Rosy trả PDF_File nhưng không phải PDF", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        """XML gốc (chữ ký người bán + MCCQT) — cùng phiên tra cứu."""
        s, row = self._search(invoice)
        r = request(s, "POST", f"{BASE}/RSeInvoiceDetail/viewXml", data=self._key(row), headers=_AJAX)
        xml = r.content or b""
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if r.status_code != 200 or not m:
            raise PdfFetchError(f"Rosy không trả XML HĐ (HTTP {r.status_code})", "no_pdf")
        if int(m.group(1)) != int(str(row.get("InvNo") or "0") or 0):
            raise PdfFetchError(f"XML Rosy tải về không phải HĐ số {row.get('InvNo')}", "bad_content")
        return xml

    # -- link trong mail -------------------------------------------------------

    def invoice_from_link(self, url: str, code, mail_text: str):
        """
        Link mail: /RSeInvoiceSearch/index?TaxCode=<MST bán>&InvID=<mã>
                   /RSeInvoiceDetail/ViewHtml?id=<mã>&taxcode=<MST bán>&PINcode=<pin>
        Chỉ đọc tham số; link trang chủ (không MST / mã) → None.
        """
        host = (urlsplit(url or "").hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        p = link_params(url)
        mst = p.get("taxcode") or ""
        inv_id = p.get("invid") or p.get("id") or (code or "").strip()
        if not _MST_RE.match(mst) or not _INVID_RE.match(inv_id):
            return None
        return {"nbmst": mst, "ma_tra_cuu": inv_id, "mst_tcgp": self.mst_tcgp[0],
                "tt_khac": {"InvID": inv_id}}


register(RosySoftAdapter())
