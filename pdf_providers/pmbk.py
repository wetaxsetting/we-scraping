"""
Bách Khoa (pmbk.vn) — 2 MSTTCGP, 2 cổng cùng nhà phát triển:
  * 0202029650 Phát triển và ứng dụng PM Bách Khoa — https://hdbk.pmbk.vn/tra-cuu-hoa-don (bản NĐ123, env.js
    ghi taxCodeTCGP 0202029650, T-VAN V0200784873)
  * 0200784873 Thiết bị điện - Điện tử Bách Khoa — https://hoadonbachkhoa.pmbk.vn/tra-cuu-hoa-don (bản cũ
    v3.5-2020)
HĐ của MSTTCGP nào cũng thử cổng mới trước rồi cổng cũ (MSTTCGP trên XML có thể là đơn vị T-VAN).

Khảo sát 27/10/2026 (đọc bundle Angular, không captcha, không reCAPTCHA, không cookie):

Cổng mới hdbk.pmbk.vn (apiURL = <host>/api/HoaDonDienTu/):
    GET  TraCuuByMa/<mã>                 → {"path": "<đường dẫn PDF>", "data": {hoaDonDienTuId, ...}}
                                            không tìm thấy → 204 (thân rỗng)
    GET  <host>/<path>                   → PDF (trang hiện bằng <embed src=fileUrl>)
    GET  GetById_TraCuu/<id>             → object HĐ (id bịa → 200 thân rỗng)
    POST TaiHoaDon_TraCuu (object HĐ)    → {"filePDF", "fileXML", "pdfName", "xmlName"} → GET <host>/<file>
    Route SPA: /tra-cuu-hoa-don/<mã>[/1] và /tra-cuu-hoa-don?hoadon=<id> (link trong mail).
    (Còn TraCuuBySoHoaDon nhận object do GetMaTraCuuInXml bóc từ file XML — không rõ tên trường, chưa dùng.)

Cổng cũ hoadonbachkhoa.pmbk.vn (baseUrl = <host>/api/bill/):
    GET  getByLookupCode/<mã>            → {"signedFileName","lookupCode","databaseName","billStatus"}
                                            không tìm thấy → 204
    PDF: <host>/uploaded/<databaseName>/pdf/signed/instances/<signedFileName>
         (billStatus 2 = đã xóa → .../pdf/deleted/<signedFileName>)

Mã tra cứu: ma_tra_cuu / TTKhac có tên chứa "tra cứu" / "MTCuu" / "LookupCode".

Đã kiểm thật đến đâu: chưa có HĐ thật (DB 27/10: 0 HĐ của 2 MSTTCGP, 0 link mail) — chỉ kiểm phản hồi
"không tìm thấy" (204) của TraCuuByMa, getByLookupCode, TraCuuBySoHoaDon bằng mã bịa. Dạng JSON khi tìm
thấy lấy từ code JS của trang, chưa thấy thật.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

NEW = "https://hdbk.pmbk.vn"
OLD = "https://hoadonbachkhoa.pmbk.vn"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _norm(key: str) -> str:
    s = unicodedata.normalize("NFD", str(key)).replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in s if c.isalnum() and not unicodedata.combining(c)).lower()


def _json(r):
    if r.status_code == 204 or not (r.content or b"").strip():
        return None
    try:
        return r.json()
    except ValueError:
        return None


class PmbkAdapter(PdfAdapter):
    source_id = "pmbk"
    name = "Bách Khoa (hdbk.pmbk.vn / hoadonbachkhoa.pmbk.vn)"
    mst_tcgp = ("0202029650", "0200784873")
    hosts = ("hdbk.pmbk.vn", "hoadonbachkhoa.pmbk.vn")

    # ------------------------------------------------------------------ mã
    @staticmethod
    def lookup_codes(invoice: dict) -> list:
        cands = [invoice.get("ma_tra_cuu")]
        for k, v in (invoice.get("tt_khac") or {}).items():
            n = _norm(k)
            if "tracuu" in n or "mtcuu" in n or "lookupcode" in n:
                cands.append(v)
        codes = (str(c).strip() for c in cands if c and str(c).strip())
        return [c for c in dict.fromkeys(codes) if _CODE_RE.match(c)]

    # ------------------------------------------------------------------ PDF
    def fetch_pdf(self, invoice: dict) -> bytes:
        s = new_session()
        hd_id = str(invoice.get("pmbk_hoadon_id") or "").strip()
        if hd_id:
            files = self._files_by_id(s, hd_id)
            pdf = self._download(s, NEW, files.get("filePDF"), "PDF")
            return self._checked(pdf, invoice)

        codes = self.lookup_codes(invoice)
        if not codes:
            raise PdfFetchError("Không có mã tra cứu (Bách Khoa pmbk)", "no_code")
        for code in codes:
            for base in self._portals(invoice):
                url = self._find_pdf_url(s, base, code)
                if url:
                    return self._checked(self._download(s, base, url, "PDF"), invoice)
        raise PdfFetchError(f"pmbk không tìm thấy HĐ với mã {', '.join(codes)}", "no_pdf")

    def fetch_xml(self, invoice: dict) -> bytes:
        """Chỉ cổng mới có API tải XML."""
        s = new_session()
        hd_id = str(invoice.get("pmbk_hoadon_id") or "").strip()
        if not hd_id:
            codes = self.lookup_codes(invoice)
            if not codes:
                raise PdfFetchError("Không có mã tra cứu (Bách Khoa pmbk)", "no_code")
            for code in codes:
                data = self._new_lookup(s, code)
                hd_id = str(((data or {}).get("data") or {}).get("hoaDonDienTuId") or "").strip()
                if hd_id:
                    break
            if not hd_id:
                raise PdfFetchError("pmbk (hdbk) không tìm thấy HĐ để tải XML", "no_pdf")
        files = self._files_by_id(s, hd_id)
        xml = self._download(s, NEW, files.get("fileXML"), "XML")
        head = xml.lstrip()[:300].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon") or b"<hdon" in head):
            raise PdfFetchError("pmbk trả nội dung không phải XML", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if shdon and (not m or int(m.group(1)) != int(shdon)):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    # ------------------------------------------------------------------ nội bộ
    @staticmethod
    def _portals(invoice: dict) -> list:
        first = invoice.get("pmbk_portal")
        order = [NEW, OLD]
        if first in order:
            order.remove(first)
            order.insert(0, first)
        return order

    @staticmethod
    def _new_lookup(s, code: str):
        r = request(s, "GET", f"{NEW}/api/HoaDonDienTu/TraCuuByMa/{quote(code, safe='')}",
                    headers={"Accept": "application/json", "Referer": f"{NEW}/tra-cuu-hoa-don"})
        if r.status_code in (204, 400, 404):
            return None
        if r.status_code != 200:
            raise PdfFetchError(f"pmbk (hdbk) TraCuuByMa trả HTTP {r.status_code}", "other")
        return _json(r)

    def _find_pdf_url(self, s, base: str, code: str) -> str | None:
        """Đường dẫn PDF (tương đối với base) hoặc None nếu cổng không có mã này."""
        if base == NEW:
            data = self._new_lookup(s, code)
            path = str((data or {}).get("path") or "").strip() if isinstance(data, dict) else ""
            return path or None
        r = request(s, "GET", f"{OLD}/api/bill/getByLookupCode/{quote(code, safe='')}",
                    headers={"Accept": "application/json", "Referer": f"{OLD}/tra-cuu-hoa-don"})
        if r.status_code in (204, 400, 404):
            return None
        if r.status_code != 200:
            raise PdfFetchError(f"pmbk (cũ) getByLookupCode trả HTTP {r.status_code}", "other")
        data = _json(r)
        if not isinstance(data, dict):
            return None
        fname, db = str(data.get("signedFileName") or "").strip(), str(data.get("databaseName") or "").strip()
        if not fname or not db:
            return None
        folder = "pdf/deleted" if str(data.get("billStatus")) == "2" else "pdf/signed/instances"
        return f"uploaded/{db}/{folder}/{fname}"

    @staticmethod
    def _files_by_id(s, hd_id: str) -> dict:
        api = f"{NEW}/api/HoaDonDienTu"
        r = request(s, "GET", f"{api}/GetById_TraCuu/{quote(hd_id, safe='')}",
                    headers={"Accept": "application/json"})
        obj = _json(r) if r.status_code == 200 else None
        if not isinstance(obj, dict) or not obj:
            raise PdfFetchError(f"pmbk (hdbk) không tìm thấy HĐ id {hd_id}", "no_pdf")
        r = request(s, "POST", f"{api}/TaiHoaDon_TraCuu", json=obj, headers={"Accept": "application/json"})
        files = _json(r) if r.status_code == 200 else None
        if not isinstance(files, dict):
            raise PdfFetchError(f"pmbk (hdbk) TaiHoaDon_TraCuu trả HTTP {r.status_code}", "other")
        return files

    @staticmethod
    def _download(s, base: str, path, what: str) -> bytes:
        path = str(path or "").strip()
        if not path:
            raise PdfFetchError(f"pmbk không trả đường dẫn file {what}", "no_pdf")
        url = path if path.lower().startswith("http") else f"{base}/{path.lstrip('/')}"
        accept = "application/pdf" if what == "PDF" else "text/xml"
        r = request(s, "GET", url, headers={"Accept": accept})
        if r.status_code == 404:
            raise PdfFetchError(f"pmbk: file {what} không tồn tại ({path})", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"pmbk tải {what} trả HTTP {r.status_code}", "other")
        return r.content or b""

    @staticmethod
    def _checked(pdf: bytes, invoice: dict) -> bytes:
        if not is_pdf(pdf):
            raise PdfFetchError("pmbk trả nội dung không phải PDF", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    # ------------------------------------------------------------------ link mail
    def invoice_from_link(self, url: str, code, mail_text: str):
        """/tra-cuu-hoa-don/<mã>[/1], ?hoadon=<id>, hoặc mã đi kèm link trong mail. Không mở link."""
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        portal = OLD if host.endswith("hoadonbachkhoa.pmbk.vn") else NEW
        params = link_params(url)
        hd_id = params.get("hoadon", "")
        if hd_id and _ID_RE.match(hd_id) and portal == NEW:
            return {"pmbk_hoadon_id": hd_id, "pmbk_portal": NEW, "tt_khac": {}}
        secret = ""
        m = re.search(r"/tra-cuu-hoa-don/([^/?#]+)", parts.path or "")
        if m:
            secret = unquote(m.group(1)).strip()
        if not secret:
            secret = params.get("matracuu") or params.get("ma") or params.get("lookupcode") or ""
        if not secret:
            secret = (code or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "pmbk_portal": portal, "tt_khac": {"Mã tra cứu": secret}}


register(PmbkAdapter())
