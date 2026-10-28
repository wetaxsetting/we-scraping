"""
Nền tảng eVAT (<đại lý>.evat.vn, PHP) — mỗi đại lý 1 subdomain, cùng 1 form tra cứu.
Đã kiểm thật: Phương Nam — MSTTCGP 0316642395 — https://phuongnam.evat.vn.

Đại lý khác cùng nền tảng (thêm 27/10/2026 theo yêu cầu user, CHƯA có HĐ thật — chỉ kiểm phản hồi
không tìm thấy): hoadonsovn / tvl / vlc / trandinhtung.evat.vn và tracuu.ttltax.com (tên miền riêng,
title "eVAT System"). Mỗi cổng: GET / ra đúng form action="/bill" (private_code + cmpn_key + CSRF
ẩn phtcsrf_tkns-<mã đại lý>), POST mã bịa không token → HTTP 200 "ERROR: Mã công ty không tồn tại",
tức CSDL riêng theo đại lý (MST người bán của Phương Nam cũng báo không tồn tại) và không bị
reCAPTCHA/đăng nhập chặn — kể cả tvl (Excel ghi "đăng nhập mới xem") và vlc (Excel ghi "lỗi máy chủ").
tracuu.evat.vn KHÔNG phải nền tảng này nữa — nay chuyển hướng sang tracuu.wininvoice.vn (MSTTCGP
0312303803, xem wininvoice.py).

Mã tra cứu (XML của eVAT có TTKhac cấp hóa đơn):
    TTKhac "Mã tra cứu hóa đơn" (Id=privateCode)  vd TC6VO2832L — cũng in trên PDF / tiêu đề mail
    TTKhac "Mã công ty"         (Id=cmpnKey)      = MST người bán (0313632864), form gọi là cmpn_key

Quy trình (khảo sát + kiểm trên HĐ thật 26/10/2026):
  1. GET  /                      → cookie phiên + input ẩn CSRF trong form action="/bill"
                                   (tên trường khác nhau theo đại lý: phtcsrf_tkns-tad, phtcsrf_tkns-tvl...)
  2. POST /bill  {private_code, cmpn_key, action=submit, <CSRF>, test=1}
       → thấy: chuyển /bill/inv, bảng HĐ có link /bill/view?name=<KHHDon>_<số 8 chữ số>&f=...&download=1
       → không thấy: ở lại /bill, "<b>ERROR: </b> Không tìm thấy hóa đơn" / "Mã công ty không tồn tại"
  3. GET  link download          → ZIP: <tên>signed.pdf + signed.xml (có chữ ký) + .html + .xslt
Trang có nạp recaptcha/api.js nhưng form không có widget, server không đòi token (gửi không token
được nhận 4/4). PDF trong ZIP trùng chữ PDF NCC gửi qua mail (2 bản chỉ khác dấu gạch mềm).
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

# MSTTCGP → cổng eVAT. Phương Nam đã kiểm bằng HĐ thật; các dòng còn lại mới kiểm form + phản hồi
# "Mã công ty không tồn tại" với mã bịa (27/10/2026), chưa có HĐ thật.
PORTALS = {
    "0316642395": "phuongnam.evat.vn",      # Phương Nam (26/10/2026: 4/4)
    "0315191291": "hoadonsovn.evat.vn",     # Trí Việt (CSRF phtcsrf_tkns-tvlgv)
    "0311622035": "tvl.evat.vn",            # Trí Việt Luật
    "0303211948": "vlc.evat.vn",            # V.L.C
    "3702037020": "trandinhtung.evat.vn",   # Trần Đình Tùng
    "0315194912": "tracuu.ttltax.com",      # TTL Tax (tên miền riêng, cùng phần mềm)
}

_FORM_RE = re.compile(r'<form[^>]*action="/bill"[^>]*>(.*?)</form>', re.S | re.I)
_HIDDEN_RE = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', re.I)
_DL_RE = re.compile(r'href="(/bill/view\?name=([^"&]+)[^"]*download=1[^"]*)"')
_ERR_RE = re.compile(r"<b>\s*ERROR:\s*</b>\s*([^<]+)", re.I)
_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,30}$")
_CMPN_RE = re.compile(r"m[ãa]\s*c[ôo]ng\s*ty\s*[:：]?\s*([0-9]{10}(?:-[0-9]{3})?)", re.I)


def _tt(invoice: dict, *names) -> str:
    tt_khac = invoice.get("tt_khac") or {}
    for n in names:
        v = str(tt_khac.get(n) or "").strip()
        if v:
            return v
    return ""


class EvatAdapter(PdfAdapter):
    source_id = "evat"
    name = "eVAT (<đại lý>.evat.vn)"
    mst_tcgp = tuple(PORTALS)
    hosts = tuple(PORTALS.values())

    # -- mã / cổng -------------------------------------------------------------

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        return _tt(invoice, "Mã tra cứu hóa đơn", "privateCode") or str(invoice.get("ma_tra_cuu") or "").strip()

    @staticmethod
    def company_key(invoice: dict) -> str:
        """cmpn_key: TTKhac "Mã công ty", thiếu thì MST người bán (đúng với mọi HĐ thật đã gặp)."""
        return _tt(invoice, "Mã công ty", "cmpnKey") or str(invoice.get("nbmst") or "").strip()

    @staticmethod
    def portal(invoice: dict) -> str:
        """Subdomain theo MSTTCGP; không có thì theo link tra cứu trong XML (nếu là *.evat.vn)."""
        mst = str(invoice.get("mst_tcgp") or "").strip()
        for key in (mst, mst.split("-")[0]):
            if key in PORTALS:
                return PORTALS[key]
        host = (urlsplit(str(invoice.get("lookup_url") or "")).hostname or "").lower()
        if host in PORTALS.values() or (host.endswith(".evat.vn") and host != "tracuu.evat.vn"):
            return host
        return ""

    # -- tải ------------------------------------------------------------------

    def fetch_pdf(self, invoice: dict) -> bytes:
        pdf = self._member(self._download_zip(invoice), ".pdf")
        if not is_pdf(pdf):
            raise PdfFetchError("ZIP eVAT có file .pdf nhưng không phải PDF", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        """XML có chữ ký người bán + CQT nằm sẵn trong ZIP tải về."""
        xml = self._member(self._download_zip(invoice), ".xml")
        shdon = str(invoice.get("shdon") or "").strip()
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if not m:
            raise PdfFetchError("XML trong ZIP eVAT không có SHDon", "bad_content")
        if shdon and int(m.group(1)) != int(shdon):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return xml

    def _download_zip(self, invoice: dict) -> zipfile.ZipFile:
        code, cmpn = self.lookup_code(invoice), self.company_key(invoice)
        if not code:
            raise PdfFetchError("XML không có TTKhac 'Mã tra cứu hóa đơn' (eVAT)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu eVAT không đúng dạng: {code!r}", "no_code")
        if not cmpn:
            raise PdfFetchError("Thiếu 'Mã công ty' (MST người bán) để tra eVAT", "no_code")
        host = self.portal(invoice)
        if not host:
            raise PdfFetchError(f"Chưa biết subdomain eVAT của MSTTCGP {invoice.get('mst_tcgp')}", "no_portal")
        base = f"https://{host}"
        s = new_session()
        r = request(s, "GET", base + "/")
        form = _FORM_RE.search(r.text or "")
        if not form:
            raise PdfFetchError(f"{host}: không thấy form tra cứu /bill", "bad_content")
        data = {"private_code": code, "cmpn_key": cmpn, "action": "submit"}
        data.update(dict(_HIDDEN_RE.findall(form.group(1))))
        r = request(s, "POST", base + "/bill", data=data, headers={"Referer": base + "/"})
        links = [(html.unescape(u), n) for u, n in _DL_RE.findall(r.text or "")]
        if not links:
            err = _ERR_RE.search(r.text or "")
            msg = html.unescape(err.group(1)).strip() if err else ""
            if "công ty" in msg.lower():
                raise PdfFetchError(f"{host}: {msg} (cmpn_key {cmpn})", "no_portal")
            if "không tìm thấy" in msg.lower():
                raise PdfFetchError(f"{host} không tìm thấy HĐ với mã {code}", "no_pdf")
            raise PdfFetchError(f"{host}: trang kết quả không có link tải ({msg or 'không có thông báo'})",
                                "bad_content")
        link = self._pick(links, invoice)
        z = request(s, "GET", base + link, headers={"Referer": getattr(r, "url", base + "/bill/inv")})
        if z.status_code != 200:
            raise PdfFetchError(f"{host} trả HTTP {z.status_code} khi tải ZIP", "bad_content")
        try:
            return zipfile.ZipFile(io.BytesIO(z.content))
        except zipfile.BadZipFile as e:
            raise PdfFetchError(f"{host} trả nội dung không phải ZIP ({z.headers.get('Content-Type')})",
                                "bad_content") from e

    @staticmethod
    def _pick(links: list, invoice: dict) -> str:
        """Nhiều dòng (hiếm) → dòng có tên <KHHDon>_<số> khớp HĐ; không xác định được thì dòng đầu."""
        shdon = str(invoice.get("shdon") or "").strip()
        for url, name in links:
            tail = name.rsplit("_", 1)[-1]
            if shdon and tail.isdigit() and int(tail) == int(shdon):
                return url
        return links[0][0]

    @staticmethod
    def _member(z: zipfile.ZipFile, ext: str) -> bytes:
        names = [n for n in z.namelist() if n.lower().endswith(ext)]
        if not names:
            raise PdfFetchError(f"ZIP eVAT không có file {ext} ({', '.join(z.namelist())})", "no_pdf")
        return z.read(names[0])

    # -- link trong mail -------------------------------------------------------

    def invoice_from_link(self, url: str, code, mail_text: str):
        """
        Mail eVAT chỉ có link trang chủ (http://phuongnam.evat.vn) + mã tra cứu. Form còn cần
        "Mã công ty" — chỉ dùng được khi nội dung mail có ghi (vd "mã công ty: 0313632864").
        Không mở link.
        """
        host = (urlsplit(url or "").hostname or "").lower()
        code = (code or "").strip()
        m = _CMPN_RE.search(mail_text or "")
        if host not in self.hosts or not _CODE_RE.match(code) or not m:
            return None
        mst_tcgp = next(k for k, v in PORTALS.items() if v == host)
        return {"ma_tra_cuu": code, "nbmst": m.group(1), "mst_tcgp": mst_tcgp,
                "tt_khac": {"Mã tra cứu hóa đơn": code, "Mã công ty": m.group(1)}}


register(EvatAdapter())
