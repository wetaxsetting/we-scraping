"""
SCTV — Truyền hình cáp SaigonTourist, MSTTCGP 0301463315 (tự phát hành HĐ cước).
Tra cứu: https://tracuu-hddt.sctv.vn/ (ASP.NET MVC, giao diện kiểu MobiFone/M-Invoice; 1 ô "Mã tra cứu").

Khảo sát 27/10/2026 (đọc script trên trang + gửi 2 mã bịa). Không captcha, không cookie:
    POST /Tracuu2/GetInfoInvoice  JSON {"sobaomat": <mã>, "masothue": "172"}   ("172" trang gắn cứng)
      → {"error": "Không tồn tại hóa đơn có Mã tra cứu hoá đơn: <mã>"}           : 'no_pdf'
      → {"isTT32": "0", "data": [{khieu, shdon, tdlap, tgtttbso, sbmat, mst}]}   : HĐ TT78
      → {"isTT32": "1", "data": [{template_code, invoice_series, invoice_number, security_number, mst}]}
    POST /Tracuu2/PrintInvoice     JSON {"sobaomat": <sbmat>, "masothue": <mst>, "type": "PDF"} → PDF
    POST /Tracuu2/ExportZipFileXML JSON {"sobaomat": <sbmat>, "masothue": <mst>}                → ZIP chứa XML
      (mã sai → 400 text/plain)

Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy. Mã: ma_tra_cuu / TTKhac kiểu "số bảo mật"/"mã tra cứu".
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

BASE = "https://tracuu-hddt.sctv.vn"
_CODE_RE = re.compile(r"^[A-Za-z0-9._-]{4,64}$")
_KEY_HINTS = ("tracuu", "baomat", "sbmat", "mtcuu", "matc", "lookup", "searchkey")


def _norm_key(key: str) -> str:
    s = unicodedata.normalize("NFD", str(key)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def lookup_codes(invoice: dict) -> list:
    tt_khac = invoice.get("tt_khac") or {}
    cands = [invoice.get("ma_tra_cuu")]
    cands += [v for k, v in tt_khac.items() if any(h in _norm_key(k) for h in _KEY_HINTS)]
    codes = (str(c).strip() for c in cands if c and str(c).strip())
    return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]


def _row_key(row: dict) -> tuple:
    """(sobaomat, mst, số HĐ) của 1 dòng kết quả, cả 2 dạng TT78 / TT32."""
    sbm = row.get("sbmat") or row.get("security_number")
    num = row.get("shdon") if row.get("shdon") is not None else row.get("invoice_number")
    return (str(sbm or "").strip(), str(row.get("mst") or "").strip(), num)


class SctvAdapter(PdfAdapter):
    source_id = "sctv"
    name = "SCTV (tracuu-hddt.sctv.vn)"
    mst_tcgp = ("0301463315",)
    hosts = ("tracuu-hddt.sctv.vn",)

    @staticmethod
    def _headers() -> dict:
        return {"Content-Type": "application/json; charset=utf-8", "Referer": f"{BASE}/",
                "X-Requested-With": "XMLHttpRequest"}

    def _lookup(self, s, invoice: dict) -> tuple:
        codes = lookup_codes(invoice)
        if not codes:
            raise PdfFetchError(f"Không có mã tra cứu ({self.name})", "no_code")
        shdon = str(invoice.get("shdon") or "").strip()
        last = ""
        for code in codes[:3]:
            r = request(s, "POST", f"{BASE}/Tracuu2/GetInfoInvoice",
                        json={"sobaomat": code, "masothue": "172"}, headers=self._headers())
            if r.status_code != 200:
                raise PdfFetchError(f"{self.name} trả HTTP {r.status_code}", "other")
            try:
                js = r.json() or {}
            except ValueError as e:
                raise PdfFetchError(f"{self.name}: trả lời không phải JSON", "bad_content") from e
            if js.get("error"):
                last = str(js["error"])
                continue
            rows = [_row_key(x) for x in (js.get("data") or []) if isinstance(x, dict)]
            rows = [x for x in rows if x[0] and x[1]]
            if shdon:
                rows = [x for x in rows if str(x[2] or "").lstrip("0") == shdon.lstrip("0")]
            if rows:
                return rows[0][0], rows[0][1]
            last = f"mã {code} không có HĐ số {shdon}" if shdon else f"mã {code} không có dữ liệu"
        raise PdfFetchError(f"{self.name} không tìm thấy HĐ ({', '.join(codes[:3])}): {last}", "no_pdf")

    def fetch_pdf(self, invoice: dict) -> bytes:
        s = new_session()
        sbm, mst = self._lookup(s, invoice)
        r = request(s, "POST", f"{BASE}/Tracuu2/PrintInvoice",
                    json={"sobaomat": sbm, "masothue": mst, "type": "PDF"}, headers=self._headers())
        if r.status_code == 400:
            raise PdfFetchError(f"{self.name}: không in được HĐ ({r.text[:100]})", "no_pdf")
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(f"{self.name} trả nội dung không phải PDF (HTTP {r.status_code})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def fetch_xml(self, invoice: dict) -> bytes:
        s = new_session()
        sbm, mst = self._lookup(s, invoice)
        r = request(s, "POST", f"{BASE}/Tracuu2/ExportZipFileXML",
                    json={"sobaomat": sbm, "masothue": mst}, headers=self._headers())
        if r.status_code == 400:
            raise PdfFetchError(f"{self.name}: không xuất được XML ({r.text[:100]})", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"{self.name} trả HTTP {r.status_code}", "other")
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if not names:
                    raise PdfFetchError(f"{self.name}: ZIP không có XML", "bad_content")
                xml = z.read(names[0])
        except zipfile.BadZipFile as e:
            raise PdfFetchError(f"{self.name} trả nội dung không phải ZIP", "bad_content") from e
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mã trong tham số link (sobaomat/ma/code) hoặc mã đi kèm mail. Không mở link."""
        p = link_params(url)
        cands = [p.get(k) for k in ("sobaomat", "sbm", "ma", "code", "matracuu")] + [code]
        secret = next((c.strip() for c in cands if c and _CODE_RE.match(c.strip())), None)
        if not secret:
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"Mã tra cứu": secret}}


register(SctvAdapter())
