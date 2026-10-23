# pdf_providers/vinhhy.py
"""
Vĩnh Hy (ehoadondientu.com) — MSTTCGP 0314743623.
Tra cứu: https://ehoadondientu.com/Tra-cuu  (1 ô "Mã hóa đơn hoặc mst", không captcha).

Nền tảng ASP.NET WebForms, dùng chung với nhà bán lại (vd Minh Khang hoadondientuvat.com, xem
minhkhang.py) — mỗi nhà một máy chủ + CSDL riêng, nên HĐ của nhà nào phải tra đúng host nhà đó.
Khảo sát 23/10/2026 trên HĐ thật (C24TPR-2087, C24TPR-1574, C24THY-58; người mua 1201496252):

    GET /Account/GenerateFile.aspx?r=ct_<database>_<stt>&type=pdf   (type=xml → XML có chữ ký)
      → 200 application/pdf, Content-Disposition "<DLHDon Id>-<số HĐ>.pdf" : thành công
      → 200 text/html, thân rỗng                                         : không có HĐ → 'no_pdf'
    Không cần cookie / phiên / captcha.

<database>_<stt> lấy thẳng từ DLHDon/@Id của XML: Id 24 chữ số = database 14 (MST bán 10 + năm 4,
vd "03158004012019") + stt 10 (vd "0300029085"). Đã đối chiếu: trang kết quả tra cứu bằng mã
trong mail ghi đúng lblData = "03158004012019", lblStt = "0300023460" cho HĐ cùng người bán.
XML không có TTKhac / mã tra cứu nào khác — ma_tra_cuu luôn rỗng, chỉ Id dùng được.

Mã tra cứu ("Mã hóa đơn", 10–11 ký tự, vd Q2EZFMHZMY) chỉ có trong mail / in trên PDF:
    POST /Tracuu.aspx/tracuu  JSON {mahoadon, loai:"mhd", lang}
      → {"d": "<html … generate_file('ct_<database>_<stt>','pdf') …>"}  : thấy HĐ
      → {"d": ""}                                                     : không thấy → 'no_pdf'
Adapter dùng đường này khi HĐ đến từ link mail (Tracuu.aspx?ma=<mã>&e=<mail>&l=Vn).
Tra theo MST thì cần chứng thư số của người bán (gọi 127.0.0.1:3110 Vsign) → không dùng.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request

# Id = database (MST 10 hoặc MST chi nhánh 13 chữ số + năm 4) + stt 10
_ID_RE = re.compile(r"^(\d{14}|\d{17})(\d{10})$")
_REF_RE = re.compile(r"generate_file\(\s*['\"](ct_\d+_\d+)['\"]\s*,\s*['\"]pdf['\"]\s*\)")
_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,20}$")


class VinhHyAdapter(PdfAdapter):
    source_id = "vinhhy"
    name = "Vĩnh Hy (ehoadondientu.com)"
    mst_tcgp = ("0314743623",)
    hosts = ("ehoadondientu.com",)
    BASE = "https://ehoadondientu.com"
    LOOKUP_PAGE = "/Tra-cuu"

    # -- mã tra cứu ------------------------------------------------------------

    @staticmethod
    def ref_from_id(dlhdon_id: str | None) -> str | None:
        """DLHDon/@Id → tham số r của GenerateFile.aspx ("ct_<database>_<stt>")."""
        m = _ID_RE.match((dlhdon_id or "").strip())
        return f"ct_{m.group(1)}_{m.group(2)}" if m else None

    def _ref(self, s, invoice: dict) -> str:
        ref = self.ref_from_id(invoice.get("dlhdon_id"))
        if ref:
            return ref
        code = (invoice.get("ma_tra_cuu") or "").strip()
        if code:
            return self._lookup(s, code)
        raise PdfFetchError(f"XML không có DLHDon/@Id dạng 24 chữ số và không có mã tra cứu "
                            f"({self.name})", "no_code")

    def _lookup(self, s, code: str) -> str:
        """Mã hóa đơn (từ mail) → "ct_<database>_<stt>" qua PageMethod tracuu."""
        url = f"{self.BASE}/Tracuu.aspx/tracuu"
        r = request(s, "POST", url, json={"mahoadon": code, "loai": "mhd", "lang": "Vn"},
                    headers={"Content-Type": "application/json; charset=utf-8",
                             "X-Requested-With": "XMLHttpRequest",
                             "Referer": f"{self.BASE}{self.LOOKUP_PAGE}"})
        if r.status_code != 200:
            raise PdfFetchError(f"{self.name} tra mã {code}: HTTP {r.status_code}", "other")
        try:
            html = r.json().get("d") or ""
        except ValueError as e:
            raise PdfFetchError(f"{self.name} tra mã {code}: không phải JSON", "bad_content") from e
        m = _REF_RE.search(html)
        if not m:
            raise PdfFetchError(f"{self.name} không tìm thấy HĐ theo mã {code}", "no_pdf")
        return m.group(1)

    # -- tải file --------------------------------------------------------------

    def _download(self, s, ref: str, kind: str) -> bytes:
        url = f"{self.BASE}/Account/GenerateFile.aspx"
        r = request(s, "GET", url, params={"r": ref, "type": kind},
                    headers={"Referer": f"{self.BASE}{self.LOOKUP_PAGE}"})
        if r.status_code != 200:
            raise PdfFetchError(f"{self.name} trả HTTP {r.status_code} cho {ref}", "other")
        if not r.content.strip():
            # HĐ không có trên máy chủ này: 200 + thân rỗng
            raise PdfFetchError(f"{self.name} không có HĐ {ref}", "no_pdf")
        return r.content

    def fetch_pdf(self, invoice: dict) -> bytes:
        s = new_session()
        ref = self._ref(s, invoice)
        pdf = self._download(s, ref, "pdf")
        if not is_pdf(pdf):
            raise PdfFetchError(f"{self.name} trả nội dung không phải PDF cho {ref}", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        """XML có chữ ký người bán + CQT (giúp khớp HĐ khi chỉ có link mail)."""
        s = new_session()
        ref = self._ref(s, invoice)
        xml = self._download(s, ref, "xml")
        head = xml.lstrip()[:200].lower()
        if not (head.startswith(b"<?xml") or head.startswith(b"<hdon")):
            raise PdfFetchError(f"{self.name} trả nội dung không phải XML cho {ref}", "bad_content")
        shdon = str(invoice.get("shdon") or "").strip()
        if shdon and f"<SHDon>{shdon}</SHDon>".encode() not in xml:
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    # -- link trong mail -------------------------------------------------------

    def invoice_from_link(self, url: str, code: str | None, mail_text: str) -> dict | None:
        """
        Nhận link tra cứu trong mail: /Tracuu.aspx?ma=<mã>&e=<mail>&l=Vn, hoặc trang /Tra-cuu kèm
        mã trong nội dung mail. Chỉ đọc tham số — không mở URL.
        """
        host = (urlsplit(url or "").hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        ma = link_params(url).get("ma") or (code or "").strip()
        if not _CODE_RE.match(ma or ""):
            return None
        return {"mst_tcgp": self.mst_tcgp[0], "ma_tra_cuu": ma, "lookup_url": url, "tt_khac": {}}


register(VinhHyAdapter())
