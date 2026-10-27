# pdf_providers/thaco.py
"""
THACO (Công ty CP Tập đoàn Trường Hải) — MSTTCGP 3600252847. Cổng HĐĐT tự vận hành của tập
đoàn, dùng cho các công ty con/liên kết (vd 0313592114, Đại Quang Minh 0310713219).
Tra cứu: https://hoadondientu.thaco.com.vn/

Form tra cứu trên trang chủ (POST /, "code" = SearchKey + captcha ảnh 194x35 chữ số, cùng kiểu
g_194x35 của PVOIL) KHÔNG cần dùng: XML có sẵn link tải thẳng, không captcha, không cookie:

    TTKhac "DownloadPdf" = "hoadondientu.thaco.com.vn/Invoice/getinvoice?token=<token>"  (không có https://)
    token = base64("<mẫu nội bộ>_<khmshdon><khhdon>_<shdon>|<MST bán>")
            vd "1C02T_1C25TAD_208|0313592114", "1C01T_1C24TQM_418|0310713219"
    GET /Invoice/getinvoice?token=<token>
        → 200 application/zip (InHoaDon_dd_mm.zip, 1 file <...>_<shdon>.pdf bên trong) : thành công
        → 302 Location /Invoice/Index                                                 : không có HĐ

"<mẫu nội bộ>" (1C01T, 1C02T...) là mã mẫu in của THACO, KHÔNG suy ra được từ XML → chỉ dùng
token có trong DownloadPdf, không tự dựng. Token cũng không có bí mật; SearchKey (ma_tra_cuu)
chỉ dùng cho form có captcha. Adapter luôn gọi BASE của mình (chỉ lấy token từ link trong XML).

Khảo sát 23/10/2026 trên HĐ thật của 0313592114 (C25TAD-208, C25TAD-173...) và 0310713219.

Mail "THÔNG TIN HÓA ĐƠN PHÍ QUẢN LÝ ... CĂN HỘ ..." (SALA, 2023–2025, kiểm 26/10): cả 27 mail mang CÙNG MỘT
link cũ http://hoadon.thaco.com.vn/Invoice/download/?id=<UTF-16 hex của chuỗi mã hóa>&pattern=01GTKT0/001
— mở tay 1 lần: luôn ra ZIP của 1 HĐ cũ mẫu TT32 (01GTKT0001_QM19E_0004670), không phải HĐ của mail.
Link cố định ở chân mail, không có khóa HĐ → nhận host để invoice_from_link trả None (link 'ignored').
"""
from __future__ import annotations

import base64
import binascii
import io
import re
import zipfile
from urllib.parse import parse_qs, urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

BASE = "https://hoadondientu.thaco.com.vn"
_TOKEN_RE = re.compile(r"getinvoice\?token=([A-Za-z0-9+/=_%-]+)", re.I)
_LINK_KEYS = ("DownloadPdf", "Downloadpdf", "downloadpdf")


def decode_token(token: str) -> dict | None:
    """token → {khmshdon, khhdon, shdon, nbmst} hoặc None nếu không đúng dạng."""
    token = (token or "").strip().replace("%3D", "=").replace("%3d", "=")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    m = re.fullmatch(r"[^_|]+_(\d)([CK]\d{2}[A-Z0-9]{3})_(\d{1,8})\|(\d{10}(?:-\d{3})?)", raw.strip())
    if not m:
        return None
    return {"khmshdon": int(m.group(1)), "khhdon": m.group(2), "shdon": int(m.group(3)),
            "nbmst": m.group(4)}


class ThacoAdapter(PdfAdapter):
    source_id = "thaco"
    name = "THACO (hoadondientu.thaco.com.vn)"
    mst_tcgp = ("3600252847",)
    # hoadon.thaco.com.vn: chỉ có link /Invoice/download cố định ở chân mail (xem trên) → None
    hosts = ("hoadondientu.thaco.com.vn", "hoadon.thaco.com.vn")

    # -- mã tra cứu ----------------------------------------------------------

    @staticmethod
    def token(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        links = [str(tt_khac.get(k) or "") for k in _LINK_KEYS]
        links += [str(invoice.get("lookup_url") or ""), str(invoice.get("thaco_token") or "")]
        for text in links:
            m = _TOKEN_RE.search(text)
            if m:
                return m.group(1)
        tok = str(invoice.get("thaco_token") or "").strip()
        if tok:
            return tok
        raise PdfFetchError("XML không có link 'DownloadPdf' (token tải HĐ THACO)", "no_code")

    # -- link trong mail -------------------------------------------------------

    def invoice_from_link(self, url: str, code: str | None, mail_text: str) -> dict | None:
        """Nhận /Invoice/getinvoice?token=.. (token base64 có ký hiệu/số/MST bán). Không mở URL."""
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        if not parts.path.lower().endswith("/invoice/getinvoice"):
            return None
        q = {k.lower(): v[0] for k, v in parse_qs(parts.query).items() if v}
        tok = (q.get("token") or "").strip()
        fields = decode_token(tok)
        if not fields:
            return None
        return {**fields, "mst_tcgp": self.mst_tcgp[0], "ma_tra_cuu": (code or "").strip() or None,
                "lookup_url": url, "tt_khac": {}, "thaco_token": tok}

    # -- tải -------------------------------------------------------------------

    def fetch_pdf(self, invoice: dict) -> bytes:
        tok = self.token(invoice)
        fields = decode_token(tok)
        if fields and invoice.get("shdon") and str(fields["shdon"]) != str(invoice["shdon"]).lstrip("0"):
            raise PdfFetchError(f"Token DownloadPdf là HĐ khác (số {fields['shdon']})", "bad_content")
        s = new_session()
        r = request(s, "GET", f"{BASE}/Invoice/getinvoice", params={"token": tok},
                    headers={"Referer": f"{BASE}/"}, allow_redirects=False)
        if r.status_code in (301, 302, 303):
            raise PdfFetchError(
                f"THACO không tìm thấy HĐ {invoice.get('khhdon')}-{invoice.get('shdon')} "
                f"của {invoice.get('nbmst')} (chuyển về {r.headers.get('Location')})", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"THACO getinvoice trả HTTP {r.status_code}", "other")
        pdf = self._extract_pdf(r.content or b"", invoice)
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    @staticmethod
    def _extract_pdf(body: bytes, invoice: dict) -> bytes:
        """Cổng trả ZIP chứa 1 PDF (đề phòng trả thẳng PDF thì nhận luôn)."""
        if is_pdf(body):
            return body
        if body[:2] != b"PK":
            raise PdfFetchError("THACO trả nội dung không phải ZIP/PDF", "bad_content")
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".pdf")]
                shdon = str(invoice.get("shdon") or "").strip().lstrip("0")
                pick = [n for n in names if shdon and n.lower().endswith(f"_{shdon}.pdf")] or names
                if not pick:
                    raise PdfFetchError("ZIP của THACO không có file PDF", "bad_content")
                pdf = z.read(pick[0])
        except (zipfile.BadZipFile, OSError) as e:
            raise PdfFetchError(f"ZIP của THACO hỏng: {e}", "bad_content") from e
        if not is_pdf(pdf):
            raise PdfFetchError("File trong ZIP của THACO không phải PDF", "bad_content")
        return pdf


register(ThacoAdapter())
