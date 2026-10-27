# pdf_providers/hilo.py
"""
T-VAN HILO — MSTTCGP 0106713804.

HILO có 2 nền tảng tra cứu (khảo sát 10/2026):

1. Nền MỚI (ASP.NET Core, cổng riêng theo khách hàng lớn) — đã chạy: Grab
   https://vn.einvoice.grab.com (XML ghi lookup_url này). Server tự trả PDF.
     1. GET  /hoa-don/tra-cuu        → __RequestVerificationToken (form GET)
     2. GET  /Captcha/Show           → SVG 250x60, 4-5 ký tự HOA/số, cookie CaptchaHash
     3. GET  /hoa-don/tra-cuu?IsFirstGet=False&__RequestVerificationToken=..&key=<Fkey>&captch=..
          lỗi: flash-message="Sai mã kiểm tra." | "Không tìm thấy hóa đơn"
          thấy: <tr id="<Fkey>"> + download-file="/Invoice/DowloadPdf?Fkey=<Fkey>"
     4. GET  /Invoice/DowloadPdf?Fkey=<Fkey>   → application/pdf (bản thể hiện, có số HĐ)
   Mã nhận HĐ = TTKhac "Fkey" (vd 26AAgwlx1vpbf1c). KHÔNG phải SearchKey (= ma_tra_cuu,
   32 hex — cổng báo "Không tìm thấy").
   Captcha là SVG: mỗi ký tự 1 <path fill=màu>, nhiễu là <path fill='none'> → bỏ nhiễu,
   dựng PNG bằng skia, đọc bằng ddddocr (không có nhóm model nào của ta khớp kiểu này).
   Số path ký tự = độ dài đáp án → đọc lệch độ dài thì lấy captcha mới, không gửi bừa.

2. Cổng CHUNG cũ https://tracuuhddt78.hilo.com.vn (MST người bán + "Mã nhận HĐ") — CHƯA làm:
   POST /Invoice/search đòi token reCAPTCHA v3 (Google) → không gọi được bằng requests.
   Thử bằng trình duyệt thật với HĐ GSM 0110269067 (Hilo-SearchKey, không có Fkey): không
   thấy HĐ (cũng không báo lỗi) → GSM có lẽ ở nền mới nhưng chưa biết địa chỉ cổng.
   → người bán chưa có cổng trong bảng: 'no_portal' (nằm chờ; bổ sung SELLER_PORTALS rồi
   requeue --status no_source --mst-tcgp 0106713804).
   NHƯNG link trong mail HILO gửi (kèm PDF in "Tra cứu hóa đơn điện tử tại: tracuuhddt78.hilo.com.vn,
   mã tra cứu: <Fkey>") tải thẳng PDF, không captcha (kiểm 23/10, Kim Đại Phát 3603526528):
     GET /Invoice/getinvoice?token=base64("<mẫu>:<ký hiệu>:<số>:<MST bán>")  → application/pdf
         (vd "1:C26TPD:1443:3603526528"); HĐ không có → 302 về /Invoice/Index
   Token chỉ gồm thông tin có sẵn trong XML → dùng cho người bán đã kiểm trong LEGACY_SELLERS và
   cho link mail (invoice_from_link).

3. Cổng cũ tt78hd.hilo.com.vn (mail 2023, kiểm 26/10 trên HĐ Times Square C23TIH-927, người mua 1201496252):
   cùng kiểu link mail nhưng token KHÁC: base64("<mẫu>_<ký hiệu>_<số>|<MST bán>"), vd "1_C23TIH_927|0300648218".
     GET /Invoice/getinvoice?token=..  → application/pdf;  HĐ không có → 302 về /Invoice/Index
   Chỉ dùng token có trong link mail (invoice_from_link), không tự dựng cho HĐ từ XML.

Link tracking.hilo.com.vn/api.php?...&taxcode=<MST bán>&custaxcode=<MST mua> trong mail HILO chỉ là link
quảng cáo/đếm click (position=footer/left/right/text_dangky, req_type=img|click) — không có khóa HĐ
→ invoice_from_link trả None (link 'ignored'); PDF lấy qua link getinvoice cùng mail.
"""
from __future__ import annotations

import base64
import binascii
import html
import re
import time
from urllib.parse import parse_qs, urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

CAPTCHA_TRIES = 4

# Cổng nền mới (host → base URL). Cùng phần mềm: /hoa-don/tra-cuu + /Captcha/Show (SVG).
PORTALS = {
    "vn.einvoice.grab.com": "https://vn.einvoice.grab.com",
}

# Người bán → host cổng, khi XML không ghi lookup_url.
SELLER_PORTALS = {
    "0312650437": "vn.einvoice.grab.com",           # Công ty TNHH Grab
}

# Cổng chung cũ: tải bằng link mail /Invoice/getinvoice?token=base64("mẫu:ký hiệu:số:MST bán").
LEGACY_BASE = "https://tracuuhddt78.hilo.com.vn"
LEGACY_HOST = "tracuuhddt78.hilo.com.vn"
# Cổng cũ tt78hd: token base64("mẫu_ký hiệu_số|MST bán") — chỉ có trong link mail
TT78HD_BASE = "https://tt78hd.hilo.com.vn"
TT78HD_HOST = "tt78hd.hilo.com.vn"
TRACKING_HOST = "tracking.hilo.com.vn"            # link đếm click/quảng cáo trong mail → bỏ qua
LEGACY_SELLERS = {
    "3603526528",                                   # Kim Đại Phát (PDF ghi tracuuhddt78; thật 1/1 + 2 PDF mail)
}

_TOKEN_RE = re.compile(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"')
_FLASH_RE = re.compile(r'flash-message="([^"]*)"')
_PDF_LINK_RE = re.compile(r'download-file="(/Invoice/DowloadPdf\?Fkey=[^"]+)"')
_PATH_RE = re.compile(r"<path\b[^>]*>", re.S)
_FILL_RE = re.compile(r"""\bfill=["']([^"']*)["']""")
_SVG_SIZE_RE = re.compile(r"""<svg\b[^>]*\bwidth=["'](\d+)["'][^>]*\bheight=["'](\d+)["']""")


class _WrongCaptcha(Exception):
    pass


def _msg(raw: str) -> str:
    # Cổng escape 2 lần: "Kh&amp;#244;ng t&amp;#236;m thấy"
    return html.unescape(html.unescape(raw)).strip().lower().replace("hoá", "hóa")


# ---------------------------------------------------------------------------
# Captcha SVG
# ---------------------------------------------------------------------------

_OCR = None


def _ocr():
    global _OCR
    if _OCR is None:
        try:
            import ddddocr
        except ImportError as e:
            raise PdfFetchError("Thiếu ddddocr (pip install ddddocr) để đọc captcha HILO", "other") from e
        _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


def _glyph_svg(svg: str) -> tuple[str, int]:
    """SVG chỉ còn các path ký tự (bỏ đường nhiễu fill='none') + số ký tự."""
    size = _SVG_SIZE_RE.search(svg)
    if not size:
        raise _WrongCaptcha("captcha không phải SVG")
    w, h = size.groups()
    glyphs = [p for p in _PATH_RE.findall(svg)
              if (_FILL_RE.search(p) or [None, "none"])[1].lower() != "none"]
    clean = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
             f'viewBox="0 0 {w} {h}">{"".join(glyphs)}</svg>')
    return clean, len(glyphs)


def _svg_to_png(svg: str, scale: float = 2.0) -> bytes:
    try:
        import skia
    except ImportError as e:
        raise PdfFetchError("Thiếu skia-python để dựng captcha SVG của HILO", "other") from e
    stream = skia.MemoryStream()
    stream.setMemory(svg.encode("utf-8"))
    dom = skia.SVGDOM.MakeFromStream(stream)
    w, h = dom.containerSize()
    if w <= 0 or h <= 0:
        raise _WrongCaptcha("SVG captcha rỗng")
    surface = skia.Surface(int(w * scale), int(h * scale))
    with surface as canvas:
        canvas.clear(skia.ColorWHITE)
        canvas.scale(scale, scale)
        dom.render(canvas)
    return bytes(surface.makeImageSnapshot().encodeToData())


def solve_svg_captcha(svg: str) -> str:
    """Đáp án captcha (HOA), hoặc _WrongCaptcha nếu đọc lệch số ký tự."""
    clean, n = _glyph_svg(svg)
    if not 4 <= n <= 6:
        raise _WrongCaptcha(f"captcha có {n} ký tự, không giống mẫu HILO")
    text = re.sub(r"[^A-Z0-9]", "", (_ocr().classification(_svg_to_png(clean)) or "").upper())
    if len(text) != n:
        raise _WrongCaptcha(f"ddddocr đọc {text!r}, captcha có {n} ký tự")
    return text


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def legacy_token(invoice: dict) -> str:
    """base64("1:C26TPD:1443:3603526528") — ký hiệu không gồm chữ số mẫu ở đầu."""
    khhdon = str(invoice.get("khhdon") or "").strip().upper()
    if len(khhdon) == 7 and khhdon[0].isdigit():     # "1C26TPD" → "C26TPD"
        khhdon = khhdon[1:]
    parts = [str(invoice.get("khmshdon") or "").strip(), khhdon,
             str(invoice.get("shdon") or "").strip().lstrip("0"), (invoice.get("nbmst") or "").strip()]
    if not all(parts):
        return ""
    return base64.b64encode(":".join(parts).encode()).decode()


def decode_legacy_token(token: str) -> dict | None:
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    m = re.fullmatch(r"(\d):([A-Z0-9]{6}):0*(\d+):(\d{10}(?:-\d{3})?)", raw.strip())
    if not m:
        return None
    return {"khmshdon": int(m.group(1)), "khhdon": m.group(2), "shdon": int(m.group(3)), "nbmst": m.group(4)}


def decode_tt78hd_token(token: str) -> dict | None:
    """base64("1_C23TIH_927|0300648218") → {khmshdon, khhdon, shdon, nbmst} hoặc None."""
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    m = re.fullmatch(r"(\d)_([A-Z0-9]{6})_0*(\d+)\|(\d{10}(?:-\d{3})?)", raw.strip())
    if not m:
        return None
    return {"khmshdon": int(m.group(1)), "khhdon": m.group(2), "shdon": int(m.group(3)), "nbmst": m.group(4)}


class HiloAdapter(PdfAdapter):
    source_id = "hilo"
    name = "T-VAN HILO"
    mst_tcgp = ("0106713804",)
    # link mail getinvoice / ViewFromEmail (2 cổng cũ); tracking = link quảng cáo, nhận để đánh 'ignored'
    hosts = (LEGACY_HOST, TT78HD_HOST, TRACKING_HOST)

    @staticmethod
    def portal_for(invoice: dict) -> str | None:
        host = urlsplit((invoice.get("lookup_url") or "").strip()).hostname or ""
        host = host.lower()
        if host in PORTALS:
            return PORTALS[host]
        seller = SELLER_PORTALS.get((invoice.get("nbmst") or "").strip())
        return PORTALS[seller] if seller else None

    @staticmethod
    def fkey(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        return (tt_khac.get("Fkey") or tt_khac.get("FKey") or "").strip()

    @staticmethod
    def is_legacy(invoice: dict) -> bool:
        host = (urlsplit((invoice.get("lookup_url") or "").strip()).hostname or "").lower()
        return host == LEGACY_HOST or (invoice.get("nbmst") or "").strip() in LEGACY_SELLERS

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Link mail tracuuhddt78: /Invoice/getinvoice|ViewFromEmail?token=.. → dict HĐ. Không mở link."""
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if host not in (LEGACY_HOST, TT78HD_HOST):          # gồm tracking.hilo.com.vn → None
            return None
        if parts.path.lower().rstrip("/") not in ("/invoice/getinvoice", "/invoice/viewfromemail"):
            return None
        tok = ({k.lower(): v[0] for k, v in parse_qs(parts.query).items() if v}.get("token") or "").strip()
        key = (code or "").strip()
        if host == TT78HD_HOST:
            fields = decode_tt78hd_token(tok)
            if not fields:
                return None
            return {**fields, "mst_tcgp": self.mst_tcgp[0], "lookup_url": f"{TT78HD_BASE}/",
                    "tt78hd_token": tok, "tt_khac": {"Fkey": key} if key else {}}
        fields = decode_legacy_token(tok)
        if not fields:
            return None
        return {**fields, "mst_tcgp": self.mst_tcgp[0], "lookup_url": f"{LEGACY_BASE}/",
                "tt_khac": {"Fkey": key} if key else {}}

    def fetch_pdf(self, invoice: dict) -> bytes:
        if invoice.get("tt78hd_token"):
            return self._getinvoice(TT78HD_BASE, invoice["tt78hd_token"], invoice)
        base = self.portal_for(invoice)
        if not base and self.is_legacy(invoice):
            return self._legacy_pdf(invoice)
        if not base:
            raise PdfFetchError(
                f"Chưa biết cổng tra cứu HILO của người bán {invoice.get('nbmst')} "
                f"({invoice.get('nbten') or '?'}, lookup_url={invoice.get('lookup_url') or '-'}) — "
                f"cổng chung tracuuhddt78.hilo.com.vn đòi reCAPTCHA v3; bổ sung PORTALS/"
                f"SELLER_PORTALS trong pdf_providers/hilo.py", "no_portal")
        fkey = self.fkey(invoice)
        if not fkey:
            raise PdfFetchError("XML không có Fkey (mã nhận hóa đơn HILO)", "no_code")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            try:
                s, link, referer = self._search(base, fkey)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"HILO từ chối/không đọc được captcha {CAPTCHA_TRIES} lần",
                                        "captcha")
                time.sleep(1)
        r = request(s, "GET", base + link, headers={"Referer": referer})
        if r.status_code != 200:
            raise PdfFetchError(f"HILO DowloadPdf trả HTTP {r.status_code}", "other")
        if not is_pdf(r.content):
            raise PdfFetchError(
                f"HILO trả nội dung không phải PDF ({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def _legacy_pdf(self, invoice: dict) -> bytes:
        tok = legacy_token(invoice)
        if not tok:
            raise PdfFetchError("Thiếu mẫu/ký hiệu/số/MST bán để dựng link tải HILO", "no_code")
        return self._getinvoice(LEGACY_BASE, tok, invoice)

    @staticmethod
    def _getinvoice(base: str, tok: str, invoice: dict) -> bytes:
        """GET <cổng cũ>/Invoice/getinvoice?token= → PDF; 302 về /Invoice/Index = không có HĐ."""
        r = request(new_session(), "GET", f"{base}/Invoice/getinvoice", params={"token": tok},
                    headers={"Referer": f"{base}/"}, allow_redirects=False)
        if r.status_code in (301, 302, 303):
            raise PdfFetchError(
                f"HILO không tìm thấy HĐ {invoice.get('khhdon')}-{invoice.get('shdon')} của "
                f"{invoice.get('nbmst')} trên {urlsplit(base).hostname}", "no_pdf")
        if r.status_code != 200:
            raise PdfFetchError(f"HILO getinvoice trả HTTP {r.status_code}", "server")
        if not is_pdf(r.content):
            raise PdfFetchError(
                f"HILO trả nội dung không phải PDF ({r.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def _search(self, base: str, fkey: str):
        """→ (session, link PDF, referer). Captcha gắn cookie phiên → phiên mới mỗi lượt."""
        s = new_session()
        page_url = f"{base}/hoa-don/tra-cuu"
        page = request(s, "GET", page_url)
        m = _TOKEN_RE.search(page.text)
        if not m:
            raise PdfFetchError(f"Không thấy form tra cứu trên {page_url}", "server")
        svg = request(s, "GET", f"{base}/Captcha/Show", headers={"Referer": page_url}).text
        answer = solve_svg_captcha(svg)
        r = request(s, "GET", page_url, headers={"Referer": page_url},
                    params={"IsFirstGet": "False", "__RequestVerificationToken": m.group(1),
                            "key": fkey, "captch": answer})
        msgs = " | ".join(_msg(x) for x in _FLASH_RE.findall(r.text) if x.strip())
        if "mã kiểm tra" in msgs:
            raise _WrongCaptcha(msgs)
        if "không tìm thấy" in msgs:
            raise PdfFetchError(f"HILO không tìm thấy HĐ với Fkey {fkey}", "no_pdf")
        links = [html.unescape(x) for x in _PDF_LINK_RE.findall(r.text)]
        exact = [x for x in links if x.endswith(f"Fkey={fkey}")]
        if not (exact or links):
            raise PdfFetchError(f"Trang kết quả HILO không có link PDF ({msgs or 'không có thông báo'})",
                                "bad_content")
        return s, (exact or links)[0], getattr(r, "url", page_url)


register(HiloAdapter())
