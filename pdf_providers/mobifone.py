# pdf_providers/mobifone.py
"""
MobiFone (Tổng công ty Viễn thông MobiFone) — MSTTCGP 0100686209 (người bán 0100686209 và chi nhánh -002...).
HĐ cước của MobiFone là hthdon=2 (Thuế không có XML) → đường chính là mã "số bảo mật" trong mail.

Khảo sát 27/10/2026 — 3 cổng cùng một phần mềm (ASP.NET, jQuery), KHÔNG captcha, không cần cookie:
  1. https://tracuuhoadonvienthong.mobifone.vn  — HĐ cước viễn thông (mail "Thông báo xuất hóa đơn điện tử
     <số>"; link .../Tracuu/PrintInvoicePdf?sobaomat=<mã> và .../Tracuu/ExportZipFileXML?sobaomat=<mã>;
     mã 6 ký tự chữ+số phân biệt hoa/thường, vd EqW7wm).
  2. https://tracuuhoadon.mobifone.vn           — HĐ bán hàng tại cửa hàng MobiFone (mail "Cửa Hàng ... thông báo
     phát hành hóa đơn điện tử số <số>"; link trang chủ trần + "số bảo mật" 7 ký tự hex trong mail, vd 7514307).
     Client gửi cứng masothue=0100686209.
  3. http://tracuuhoadon.mobifoneinvoice.vn     — cổng chung cho người bán KHÁC dùng phần mềm MobiFone Invoice:
     form có ô MST người bán + số bảo mật. Với MST 0100686209 server lỗi
     ("relation db_test_ttcntt.inv_invoiceauth does not exist"); https timeout → dùng http.
     Chưa có HĐ thật của người bán khác → CHƯA KIỂM thật, adapter chỉ dùng khi nbmst không phải MobiFone.

API (POST JSON, cả 3 cổng):
    /Tracuu2/GetInfoInvoice   {sobaomat, masothue, typeSearch:"hoadon"}
        → {"data":[{security_number|sbmat, invoice_number|shdon, invoice_series|khieu, mst, ...}]}
        → {"error":"Không tồn tại hóa đơn có số bảo mật: X"}           : không thấy → 'no_pdf'
    /Tracuu2/PrintInvoice     {sobaomat, masothue, type:"PDF", typeSearch} → application/pdf
        → HTTP 400 "Không tồn tại hóa đơn có số bảo mật X"
    /Tracuu2/ExportZipFileXML {sobaomat, masothue, typeSearch} → ZIP {invoice.xml (có chữ ký), *.pdf, *.repx}
masothue lấy từ trường "mst" của GetInfoInvoice. Mỗi cổng có CSDL riêng: mã cổng này tra ở cổng kia
ra "Không tồn tại" → mã không rõ cổng thì thử lần lượt (1) rồi (2).

Đã kiểm thật 27/10/2026 bằng mã trong mail (HĐ chưa có trong DB vì hthdon=2 / công ty khác trong nhóm):
viễn thông 1K23DAD-1213289 (EqW7wm) PDF + XML; cửa hàng 1K25TVB-1216 (7514307) PDF — PDF đều chứa đúng số HĐ.
XML MobiFone không có số bảo mật (TTKhac chỉ có tthue/tgtien/charging_from...), nên HĐ từ XML Thuế → 'no_code'.
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

MOBIFONE_MST = "0100686209"
TELECOM = "https://tracuuhoadonvienthong.mobifone.vn"
STORE = "https://tracuuhoadon.mobifone.vn"
PARTNER = "http://tracuuhoadon.mobifoneinvoice.vn"
PORTAL_BY_HOST = {
    "tracuuhoadonvienthong.mobifone.vn": TELECOM,
    "tracuuhoadon.mobifone.vn": STORE,
    "tracuuhoadon.mobifoneinvoice.vn": PARTNER,
}
CODE_KEYS = ("Số bảo mật", "So bao mat", "SoBaoMat", "sobaomat", "Mã tra cứu")
_CODE_RE = re.compile(r"^[A-Za-z0-9]{5,12}$")
_SUBJECT_NO_RE = re.compile(r"(?i)h[oó]a\s*đơn\s*điện\s*tử\s*(?:số\s*)?:?\s*0*(\d{1,10})\b")
_NOT_FOUND = "không tồn tại"


class MobifoneAdapter(PdfAdapter):
    source_id = "mobifone"
    name = "MobiFone (tracuuhoadon*.mobifone.vn)"
    mst_tcgp = (MOBIFONE_MST,)
    hosts = tuple(PORTAL_BY_HOST)

    # -- mã + cổng -------------------------------------------------------------

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for k in CODE_KEYS:
            if str(tt_khac.get(k) or "").strip():
                return str(tt_khac[k]).strip()
        return str(invoice.get("ma_tra_cuu") or "").strip()

    @staticmethod
    def portals(invoice: dict) -> list:
        """(base, masothue gửi lên) theo thứ tự thử."""
        host = (urlsplit(invoice.get("lookup_url") or "").hostname or "").lower()
        if host in PORTAL_BY_HOST:
            base = PORTAL_BY_HOST[host]
            mst = (invoice.get("nbmst") or "") if base == PARTNER else MOBIFONE_MST
            return [(base, mst)]
        nbmst = str(invoice.get("nbmst") or "").strip()
        if nbmst and not nbmst.startswith(MOBIFONE_MST):
            return [(PARTNER, nbmst)]          # người bán khác dùng phần mềm MobiFone Invoice
        return [(TELECOM, MOBIFONE_MST), (STORE, MOBIFONE_MST)]

    def _code(self, invoice: dict) -> str:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("Không có số bảo mật MobiFone (chỉ có trong mail NCC gửi)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Số bảo mật MobiFone không đúng dạng: {code!r}", "no_code")
        return code

    def _info(self, s, base: str, code: str, mst: str) -> dict | None:
        """GetInfoInvoice → dòng HĐ, None nếu cổng này không có mã đó."""
        r = request(s, "POST", f"{base}/Tracuu2/GetInfoInvoice",
                    json={"sobaomat": code, "masothue": mst, "typeSearch": "hoadon"},
                    headers={"Referer": f"{base}/", "X-Requested-With": "XMLHttpRequest"})
        if r.status_code != 200:
            raise PdfFetchError(f"{self.name} tra mã {code}: HTTP {r.status_code}", "other")
        try:
            j = r.json()
        except ValueError as e:
            raise PdfFetchError(f"{self.name} tra mã {code}: không phải JSON", "bad_content") from e
        err = str(j.get("error") or "")
        if err:
            if _NOT_FOUND in err.lower():
                return None
            raise PdfFetchError(f"{base} lỗi khi tra mã {code}: {err[:200]}", "server")
        rows = j.get("data") or []
        return rows[0] if rows else None

    def _locate(self, s, invoice: dict) -> tuple:
        """→ (base, sobaomat, masothue) của cổng có HĐ."""
        code = self._code(invoice)
        for base, mst in self.portals(invoice):
            row = self._info(s, base, code, mst)
            if row is None:
                continue
            shdon = str(invoice.get("shdon") or "").strip()
            got = str(row.get("invoice_number") or row.get("shdon") or "").strip()
            if shdon and got and int(got) != int(shdon):
                raise PdfFetchError(f"Mã {code} là HĐ số {got}, không phải {shdon}", "bad_content")
            sbm = str(row.get("security_number") or row.get("sbmat") or code)
            return base, sbm, str(row.get("mst") or mst)
        raise PdfFetchError(f"{self.name} không tìm thấy HĐ số bảo mật {code}", "no_pdf")

    def _post(self, s, base: str, path: str, body: dict):
        r = request(s, "POST", f"{base}/Tracuu2/{path}", json=body, headers={"Referer": f"{base}/"})
        if r.status_code == 400 and _NOT_FOUND in (r.text or "").lower():
            raise PdfFetchError(f"{self.name} không có HĐ số bảo mật {body['sobaomat']}", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"{self.name} {path}: HTTP {r.status_code}", "other")
        return r.content

    # -- tải file --------------------------------------------------------------

    def fetch_pdf(self, invoice: dict) -> bytes:
        s = new_session()
        base, sbm, mst = self._locate(s, invoice)
        pdf = self._post(s, base, "PrintInvoice",
                         {"sobaomat": sbm, "masothue": mst, "type": "PDF", "typeSearch": "hoadon"})
        if not is_pdf(pdf):
            raise PdfFetchError(f"{self.name} trả nội dung không phải PDF cho mã {sbm}", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        """XML có chữ ký, nằm trong ZIP của ExportZipFileXML."""
        s = new_session()
        base, sbm, mst = self._locate(s, invoice)
        data = self._post(s, base, "ExportZipFileXML",
                          {"sobaomat": sbm, "masothue": mst, "typeSearch": "hoadon"})
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if not names:
                    raise PdfFetchError(f"ZIP MobiFone không có XML (mã {sbm})", "bad_content")
                xml = z.read(names[0])
        except zipfile.BadZipFile as e:
            raise PdfFetchError(f"{self.name} không trả ZIP cho mã {sbm}", "bad_content") from e
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if not m:
            raise PdfFetchError(f"XML MobiFone không có số HĐ (mã {sbm})", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        if shdon and int(m.group(1)) != int(shdon):
            raise PdfFetchError(f"XML MobiFone tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    # -- link trong mail -------------------------------------------------------

    def invoice_from_link(self, url: str, code: str | None, mail_text: str) -> dict | None:
        """
        Link .../Tracuu/PrintInvoicePdf?sobaomat=<mã> (hoặc ExportZipFileXML) của cổng viễn thông, hoặc
        link trang chủ cổng cửa hàng kèm "số bảo mật" trong mail. Số HĐ lấy từ tiêu đề mail
        (đọc nhầm thì check_pdf_is_invoice / so số HĐ chặn). Chỉ đọc tham số — không mở URL.
        """
        host = (urlsplit(url or "").hostname or "").lower()
        if host not in PORTAL_BY_HOST:
            return None
        sbm = (link_params(url).get("sobaomat") or code or "").strip()
        if not _CODE_RE.match(sbm):
            return None
        inv = {"mst_tcgp": MOBIFONE_MST, "ma_tra_cuu": sbm, "lookup_url": url,
               "tt_khac": {"Số bảo mật": sbm}}
        if PORTAL_BY_HOST[host] != PARTNER:
            inv["nbmst"] = MOBIFONE_MST
        m = _SUBJECT_NO_RE.search(unicodedata.normalize("NFC", mail_text or ""))
        if m:
            inv["shdon"] = m.group(1)
        return inv


register(MobifoneAdapter())
