# pdf_providers/thaison.py
"""
Thái Sơn E-invoice (einvoice.vn) — MSTTCGP 0101300842.

Mã tra cứu = TTKhac "Mã TC" ("Mã nhận hóa đơn" trên cổng; vd 69AEIZEURC3, VSOJKEFHUPMQSAHR).
Địa chỉ cổng = TTKhac "DC TC" (= lookup_url của parser). Mỗi khách hàng lớn có thể có CỔNG RIÊNG,
cùng phần mềm Thái Sơn nhưng form khác → bảng PORTALS (host → kiểu cổng). Khảo sát 10/2026 trên HĐ thật:

  * einvoice.vn (cổng chung, kiểu "generic") — Jollibee 0303883266, Gốm sứ Kiến An 3700145856
      1. GET  /tra-cuu                      → CaptchaDeText (32 hex) trong form
      2. GET  /DefaultCaptcha/Generate?t=…  → ảnh 200x70, 4 chữ HOA → model g_200x70
           ⚠ BẮT BUỘC có header Referer = trang tra cứu; thiếu Referer server trả ảnh lỗi
             (GIF ~1,2 KB, dấu X đỏ) thay vì captcha.
      3. POST /tra-cuu   MaNhanHoaDon, CaptchaDeText, CaptchaInputText
           → lỗi trong <div class="… fix-text-danger">: "Mã kiểm tra không đúng." (sai captcha)
             | "Mã nhận hóa đơn không đúng." (không thấy); thấy: IsCaptchaValid value="true"
      4. GET  /tra-cuu/tai-hoa-don-dien-tu?format=pdf → ZIP chứa <mẫu>_<ký hiệu>_<số>.pdf
           (theo phiên/cookie; format=xml → ZIP chứa XML)
  * einvoice.mmvietnam.com (MM Mega Market 0302249586, kiểu "store")
      1. GET  /                              → CaptchaDeText (cùng CaptchaMvc, cũng cần Referer)
      2. POST /xem-hoa-don   MA_NHAN_HOA_DON, CaptchaDeText, CaptchaInputText
           → lỗi: "Error: Mã bảo mật không đúng!" (sai captcha)
             | "Không tìm thấy hóa đơn có thông tin vừa nhập!"; thấy: /tai-ve-hoa-don/<guid>
      3. GET  /tai-ve-hoa-don/<guid>        → ZIP {xml, xslt, pdf} — bản gốc ("Download HĐ").
           (?printFlag=…: bản chuyển đổi, không dùng)
Server KIỂM captcha thật ở cả 2 cổng. Không có reCAPTCHA.

Cổng riêng thêm 23/10/2026 (cùng phần mềm, cùng POST /xem-hoa-don + MA_NHAN_HOA_DON, gửi kèm
__RequestVerificationToken của form). Trang kết quả có form tải "/Download/DownloadFile"
(__RequestVerificationToken + idHoaDon mã hóa) → ZIP {xml, xslt, pdf, *_hdcd.pdf}; lấy bản gốc
<…>.pdf, KHÔNG lấy *_hdcd.pdf (hóa đơn chuyển đổi):
  * vat.ggg.com.vn (Cổng Vàng / Golden Gate 0102721191-…, PDF ghi "tra cứu tại http://vat.ggg.com.vn/")
      kiểu "store": CaptchaMvc như MM Mega Market → g_200x70.
  * invoice.kfcvietnam.com.vn (KFC 0100773885-…; PDF ghi "www.invoice.kfcvietnam.com.vn" — www. trả 404)
    invoice.zaloplatforms.vn (Zalo 0318836678, TTKhac "ĐCTC")
      kiểu "store_img": GET /Captcha → PNG 200x60 RGBA, 6 ký tự hoa/thường/số, chữ xanh nghiêng
      font cố định + chấm nhiễu màu. Server KIỂM thật và phân biệt hoa/thường
      ("Mã kiểm tra không đúng!"); trường captchaCode. Không có model nào của ta → ddddocr trên
      ảnh đã bỏ nhiễu, rồi sửa hoa/thường theo hình học glyph (ddddocr hay đọc sai case:
      'aIZmo7' cho 'AIZMo7'). Đo 23/10: server nhận 44/51 lần gửi (86%; lỗi chủ yếu ở I/l — hai chữ giống
      hệt nhau trong font này), 6/57 ảnh bị bỏ không gửi vì đọc lệch số ký tự.
  * hddt.cellphones.com.vn (CellphoneS = Diệu Phúc 0316172372)
      kiểu "store_js": "captcha" chỉ là chuỗi JS sinh ở trình duyệt (MA_XAC_THUC, 5 ký tự), server
      không kiểm → gửi chuỗi ngẫu nhiên.
  * Grab 0312650437 (HĐ Thái Sơn 2023–2025; nay Grab dùng HILO): CHƯA tra được → SELLER_UNSUPPORTED,
    'no_portal' ngay. Cổng HILO vn.einvoice.grab.com tìm ra HĐ 2025 (C25TAA, 5/5) theo "Mã TC" như Fkey
    nhưng /Invoice/DowloadPdf|DowloadXml|DowloadConvertPdf đều HTTP 400 {"success":false} (HĐ HILO
    thật thì tải được) → HĐ chuyển sang chỉ có dòng, không có file. HĐ 2023–2024 (C23TGN, C24TGN,
    C24TAA) không có ở HILO; einvoice.vn báo "Mã nhận hóa đơn không đúng"; grab.einvoice.vn/.com.vn 404.

Định tuyến: host của "DC TC"/"ĐCTC"/lookup_url → PORTALS; XML không ghi thì theo MST người bán
(SELLER_PORTALS, MST chi nhánh "-001" dùng MST gốc). Không có cả hai → thử cổng chung einvoice.vn;
không thấy ở đó → 'no_portal' (HĐ nằm chờ, bổ sung cổng rồi requeue --status no_source
--mst-tcgp 0101300842). Host lạ (cổng riêng chưa khảo sát) → 'no_portal'.
Cổng riêng tìm theo dòng "Mã nhận hóa đơn …: … tra cứu tại (Tracking): <host>" in cuối PDF
(PDF trong output/mail/unmatched) — XML của KFC/Cổng Vàng/Diệu Phúc/Grab không ghi cổng.
"""
from __future__ import annotations

import html
import io
import logging
import random
import re
import string
import unicodedata
import zipfile
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, link_params, new_session, request, solve_captcha
from .ocr_ddddocr import ocr_text

log = logging.getLogger(__name__)

CAPTCHA_GROUP = "g_200x70"
CAPTCHA_LEN = 4
CAPTCHA_MIN_CONF = 0.8
CAPTCHA_TRIES = 5
IMG_CAPTCHA_TRIES = 8             # store_img: ~80% lần gửi qua, ~15% ảnh bỏ không gửi
IMG_CAPTCHA_LEN = (4, 8)
GENERIC_HOST = "einvoice.vn"

# host cổng → (kiểu cổng, base URL)
PORTALS = {
    "einvoice.vn": ("generic", "https://einvoice.vn"),
    "einvoice.mmvietnam.com": ("store", "https://einvoice.mmvietnam.com"),
    "vat.ggg.com.vn": ("store", "https://vat.ggg.com.vn"),
    "invoice.kfcvietnam.com.vn": ("store_img", "https://invoice.kfcvietnam.com.vn"),
    "invoice.zaloplatforms.vn": ("store_img", "https://invoice.zaloplatforms.vn"),
    "hddt.cellphones.com.vn": ("store_js", "https://hddt.cellphones.com.vn"),
}

# Người bán → host cổng, khi XML không ghi "DC TC".
SELLER_PORTALS = {
    "0302249586": "einvoice.mmvietnam.com",     # MM Mega Market
    "0303883266": "einvoice.vn",                # Jollibee
    "3700145856": "einvoice.vn",                # Gốm sứ Mỹ nghệ Kiến An
    "0102721191": "vat.ggg.com.vn",             # Cổng Vàng / Golden Gate (chi nhánh -001)
    "0100773885": "invoice.kfcvietnam.com.vn",  # KFC Việt Nam (chi nhánh -001)
    "0318836678": "invoice.zaloplatforms.vn",   # Zalo Platforms
    "0316172372": "hddt.cellphones.com.vn",     # Diệu Phúc (CellphoneS)
}

# Cổng biết là của Thái Sơn nhưng chưa làm được: host → lý do (ghi trong lỗi 'no_portal').
UNSUPPORTED: dict = {}

# Người bán biết là không tra được ở cổng nào → 'no_portal' ngay, không gọi mạng.
SELLER_UNSUPPORTED = {
    "0312650437": "HĐ Thái Sơn 2023–2025 của Grab: chưa tìm thấy cổng Thái Sơn riêng của Grab; cổng HILO "
                  "vn.einvoice.grab.com liệt kê được HĐ 2025 theo Mã TC nhưng tải PDF/XML đều HTTP 400 "
                  "(không có file), HĐ 2023–2024 không có; einvoice.vn báo không có",
}

_DETEXT_RE = re.compile(r'id="CaptchaDeText"[^>]*value="([0-9a-fA-F]{32})"')
_TOKEN_RE = re.compile(r'name="__RequestVerificationToken" type="hidden" value="([^"]+)"')
_GENERIC_ERR_RE = re.compile(r'fix-text-danger">([^<]*)<')
_GENERIC_OK_RE = re.compile(r'id="IsCaptchaValid" value="true"', re.I)
_STORE_GUID_RE = re.compile(r"/tai-ve-hoa-don/([0-9a-fA-F-]{36})")
_STORE_DL_RE = re.compile(r'<form action="(/Download/DownloadFile)"[^>]*>(.*?)</form>', re.S)
_STORE_ERR_RE = re.compile(r"Error:\s*(?:</strong>\s*)?([^<]+)<")
_HIDDEN_RE = re.compile(r'<input\b[^>]*\bname="([^"]+)"[^>]*\bvalue="([^"]*)"')
_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,30}$")
_MAIL_CODE_RE = re.compile(r"m[ãa]\s*(?:nh[ậa]n\s*h[óo]a\s*[đd][ơo]n|tra\s*c[ứu]u|tc)\s*[:：]\s*([A-Za-z0-9]{6,30})",
                           re.I)
_CODE_KEYS = ("matc", "manhanhoadon", "manhanhd", "matracuu")
_PORTAL_KEYS = ("dctc", "diachitracuu")


class _WrongCaptcha(Exception):
    pass


class _NotFound(Exception):
    pass


def _norm_key(k: str) -> str:
    """'Mã TC' / 'ĐCTC' / 'DC TC' → 'matc' / 'dctc' (bỏ dấu, khoảng trắng, chữ thường)."""
    k = unicodedata.normalize("NFD", str(k).replace("Đ", "D").replace("đ", "d"))
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in k if not unicodedata.combining(c)).lower())


def _host(url: str | None) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _tt(invoice: dict, keys: tuple) -> str:
    for k, v in (invoice.get("tt_khac") or {}).items():
        if _norm_key(k) in keys and str(v or "").strip():
            return str(v).strip()
    return ""


def _pdf_from_zip(data: bytes, what: str) -> bytes:
    """PDF bản gốc trong ZIP. *_hdcd.pdf (hóa đơn chuyển đổi) chỉ lấy khi không có bản gốc."""
    if is_pdf(data):
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".pdf")]
            names.sort(key=lambda n: n.lower().endswith("_hdcd.pdf"))
            pdf = z.read(names[0]) if names else b""
    except zipfile.BadZipFile:
        pdf = b""
    if not is_pdf(pdf):
        raise PdfFetchError(f"Thái Sơn ({what}) trả file không có PDF ({len(data)} byte)", "bad_content")
    return pdf


# --- captcha ảnh /Captcha (kiểu store_img: KFC, Zalo) -------------------------------------------
# Ảnh 200x60: chữ (0,0,255) nghiêng, khử răng cưa bằng alpha → trải lên nền trắng thành điểm
# (v,v,255); chấm nhiễu là điểm màu khác (R≠G) hoặc cụm < 8 điểm. Đo trên 40 ảnh: đỉnh chữ HOA /
# số / chữ có nét trên ở y 16–18, đỉnh chữ x-height 24–25 ('t' 20); đáy chữ thò xuống (g j p q y)
# ở 54, còn lại 46–47. Chiều rộng tách K/k, T/t, F/f, B/b, D/d, H/h, 0/O, L/l.

def _img_mask(img: bytes):
    import cv2
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(img)).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    a = np.asarray(bg.convert("RGB")).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = ((abs(r - g) <= 8) & (r < 150) & (b > 200)).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    for k in range(1, n):
        if st[k, cv2.CC_STAT_AREA] < 8:
            m[lab == k] = 0
    return m


def _img_glyphs(m) -> list:
    """Thành phần liên thông cao ≥ 10px = 1 ký tự (trái → phải); chấm của i/j gắn vào glyph dưới nó."""
    import cv2
    n, _, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    comps = [{"x": int(st[k, 0]), "y": int(st[k, 1]), "w": int(st[k, 2]), "h": int(st[k, 3])}
             for k in range(1, n)]
    big = sorted((c for c in comps if c["h"] >= 10), key=lambda c: c["x"])
    for dot in (c for c in comps if c["h"] < 10):
        cx = dot["x"] + dot["w"] / 2
        near = min(big, key=lambda c: abs(c["x"] + c["w"] / 2 - cx), default=None)
        if near and abs(near["x"] + near["w"] / 2 - cx) < 12 and dot["y"] < near["y"]:
            near["dot"] = True
    return big


def _fix_case(c: str, g: dict) -> str:
    """Sửa hoa/thường (và 0/O/o, i/l/L) của ký tự ddddocr đọc, theo hình học glyph."""
    top, bottom, w = g["y"], g["y"] + g["h"], g["w"]
    tall, desc, low = top <= 21, bottom >= 50, c.lower()
    if c == "0" or low == "o":
        return ("0" if w <= 24 else "O") if tall else "o"
    if low in "acemnrsuvwxz":                 # hoa/thường cùng dáng, chỉ khác chiều cao
        return low.upper() if tall else low
    if low in "gjpqy":                        # chữ thường thò xuống dưới đường cơ sở
        return low if desc else low.upper()
    if low == "k":
        return "K" if w >= 28 else "k"
    if low == "t":
        return "T" if w >= 18 else "t"
    if low == "f":
        return "F" if w >= 23 else "f"
    if low in "bdh":
        return low.upper() if w >= 26 else low
    if low in "il":
        if g.get("dot"):
            return "i"
        if low == "l" and w >= 16:
            return "L"
        return "l" if c == "i" else c          # I và l giống hệt nhau trong font này: giữ cái ddddocr đọc
    return c


def solve_img_captcha(img: bytes) -> str:
    """Đáp án captcha /Captcha (giữ hoa/thường), hoặc _WrongCaptcha nếu đọc không khớp số ký tự."""
    try:
        from PIL import Image
        m = _img_mask(img)
        glyphs = _img_glyphs(m)
        buf = io.BytesIO()
        Image.fromarray(((1 - m) * 255).astype("uint8")).save(buf, format="PNG")
    except Exception as e:  # noqa: BLE001
        raise _WrongCaptcha(f"ảnh captcha không đọc được: {e}") from e
    text = re.sub(r"[^A-Za-z0-9]", "", ocr_text(buf.getvalue()))
    if len(text) != len(glyphs) or not IMG_CAPTCHA_LEN[0] <= len(text) <= IMG_CAPTCHA_LEN[1]:
        raise _WrongCaptcha(f"ddddocr đọc {text!r}, ảnh có {len(glyphs)} ký tự")
    return "".join(_fix_case(c, g) for c, g in zip(text, glyphs))


class ThaisonAdapter(PdfAdapter):
    source_id = "thaison"
    name = "Thái Sơn E-invoice"
    mst_tcgp = ("0101300842",)
    hosts = tuple(PORTALS)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """
        Mail Thái Sơn: link trang tra cứu (einvoice.vn/tra-cuu, einvoice.mmvietnam.com, …) — link không
        mang mã; mã ("Mã nhận hóa đơn: …") nằm trong nội dung mail. Chỉ đọc chuỗi, không mở link.
        """
        host = _host(url)
        if host not in self.hosts:
            return None
        p = link_params(url)
        key = (code or p.get("manhanhoadon") or p.get("ma_nhan_hoa_don") or "").strip()
        if not key:
            m = _MAIL_CODE_RE.search(mail_text or "")
            if m:
                key = m.group(1)
            else:                             # process_links: dòng 1 = tiêu đề, sau đó mỗi dòng 1 mã
                bare = [ln.strip() for ln in (mail_text or "").splitlines()[1:]
                        if _CODE_RE.match(ln.strip()) and len(ln.strip()) >= 8]
                key = bare[0] if len(bare) == 1 else ""
        if not _CODE_RE.match(key):
            return None
        base = PORTALS[host][1]
        inv = {"ma_tra_cuu": key, "tt_khac": {"Mã TC": key, "DC TC": base}, "lookup_url": base}
        msts = [m for m, h in SELLER_PORTALS.items() if h == host]
        if host != GENERIC_HOST and len(msts) == 1:
            inv["nbmst"] = msts[0]
        return inv

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        return _tt(invoice, _CODE_KEYS) or str(invoice.get("ma_tra_cuu") or "").strip()

    @staticmethod
    def portal_for(invoice: dict) -> tuple[str | None, bool]:
        """(host cổng, có phải đoán không). host None = cổng lạ/chưa làm → no_portal."""
        host = _host(_tt(invoice, _PORTAL_KEYS) or invoice.get("lookup_url"))
        if host:
            return (host if host in PORTALS and host not in UNSUPPORTED else None), False
        mst = str(invoice.get("nbmst") or "").strip()
        for key in dict.fromkeys((mst, mst.split("-")[0])):
            if key in SELLER_UNSUPPORTED:
                return None, False
            if key in SELLER_PORTALS:
                return SELLER_PORTALS[key], False
        return GENERIC_HOST, True             # không biết cổng → thử cổng chung

    def fetch_pdf(self, invoice: dict) -> bytes:
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có 'Mã TC' (mã nhận hóa đơn Thái Sơn)", "no_code")
        host, guessed = self.portal_for(invoice)
        if host is None:
            raw = _host(_tt(invoice, _PORTAL_KEYS) or invoice.get("lookup_url"))
            mst = str(invoice.get("nbmst") or "").strip()
            why = (UNSUPPORTED.get(raw) or SELLER_UNSUPPORTED.get(mst) or SELLER_UNSUPPORTED.get(mst.split("-")[0])
                   or "cổng chưa khảo sát")
            raise PdfFetchError(f"Thái Sơn: cổng {raw or '?'} của người bán {mst} — {why}", "no_portal")
        kind, base = PORTALS[host]
        tries = IMG_CAPTCHA_TRIES if kind == "store_img" else CAPTCHA_TRIES
        for attempt in range(1, tries + 1):
            s = new_session()
            try:
                pdf = self._generic(s, base, code) if kind == "generic" else self._store(s, base, code, kind)
                break
            except _WrongCaptcha as e:
                log.info("Thái Sơn %s: captcha lần %d không qua (%s)", host, attempt, e)
                if attempt == tries:
                    raise PdfFetchError(f"Thái Sơn ({host}) từ chối captcha {tries} lần", "captcha")
            except _NotFound as e:
                if guessed:
                    raise PdfFetchError(
                        f"Thái Sơn: XML không ghi cổng, cổng chung {host} không có HĐ mã {code} "
                        f"(người bán {invoice.get('nbmst')} có lẽ có cổng riêng)", "no_portal") from e
                raise PdfFetchError(f"Thái Sơn ({host}) không tìm thấy HĐ mã {code}: {e}", "no_pdf") from e
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    # --- captcha CaptchaMvc (generic + store) -----------------------------------------------

    @staticmethod
    def _captcha(s, page_url: str, base: str, page: str | None = None) -> tuple[str, str]:
        """Trang form → (CaptchaDeText, đáp án). Model không chắc → _WrongCaptcha (không gửi bừa)."""
        if page is None:
            page = request(s, "GET", page_url).text or ""
        m = _DETEXT_RE.search(page)
        if not m:
            raise PdfFetchError(f"Thái Sơn: trang {page_url} không có captcha", "server")
        t = m.group(1)
        img = request(s, "GET", f"{base}/DefaultCaptcha/Generate", params={"t": t},
                      headers={"Referer": page_url,
                               "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}).content
        if len(img or b"") < 1500:            # ảnh lỗi (dấu X đỏ) — thiếu Referer / mã hết hạn
            raise _WrongCaptcha(f"server trả ảnh captcha lỗi ({len(img or b'')} byte)")
        try:
            answer = solve_captcha(img, CAPTCHA_GROUP, min_conf=CAPTCHA_MIN_CONF)
        except PdfFetchError as e:
            raise _WrongCaptcha(str(e)) from e
        if not (len(answer) == CAPTCHA_LEN and answer.isalpha()):
            raise _WrongCaptcha(f"model đọc {answer!r}, captcha Thái Sơn là {CAPTCHA_LEN} chữ cái")
        return t, answer.upper()

    # --- cổng chung einvoice.vn -------------------------------------------------------------

    def _generic(self, s, base: str, code: str) -> bytes:
        page = f"{base}/tra-cuu"
        t, answer = self._captcha(s, page, base)
        r = request(s, "POST", page, headers={"Referer": page, "Origin": base},
                    data={"MaNhanHoaDon": code, "CaptchaDeText": t, "CaptchaInputText": answer})
        errors = {html.unescape(e).strip() for e in _GENERIC_ERR_RE.findall(r.text or "")} - {""}
        low = " ".join(errors).lower()
        if "mã kiểm tra" in low:
            raise _WrongCaptcha(low)
        if "mã nhận hóa đơn" in low or "không tìm thấy" in low:
            raise _NotFound(low)
        if errors or not _GENERIC_OK_RE.search(r.text or ""):
            raise PdfFetchError(f"Thái Sơn einvoice.vn trả kết quả lạ (HTTP {r.status_code}): {low}"[:300],
                                "other")
        d = request(s, "GET", f"{base}/tra-cuu/tai-hoa-don-dien-tu", params={"format": "pdf"},
                    headers={"Referer": page}, timeout=60)
        return _pdf_from_zip(d.content, base)

    # --- cổng riêng kiểu /xem-hoa-don (MM Mega Market, Cổng Vàng, KFC, Zalo, CellphoneS) ----

    def _store(self, s, base: str, code: str, kind: str = "store") -> bytes:
        page = f"{base}/"
        form = request(s, "GET", page).text or ""
        data = {"MA_NHAN_HOA_DON": code}
        tok = _TOKEN_RE.search(form)
        if tok:
            data["__RequestVerificationToken"] = html.unescape(tok.group(1))
        if kind == "store":
            t, answer = self._captcha(s, page, base, form)
            data.update({"CaptchaDeText": t, "CaptchaInputText": answer})
        elif kind == "store_img":
            img = request(s, "GET", f"{base}/Captcha", headers={"Referer": page}).content
            data["captchaCode"] = solve_img_captcha(img or b"")
        else:                                 # store_js: "captcha" chỉ kiểm ở trình duyệt
            data["MA_XAC_THUC"] = "".join(random.choices(string.ascii_letters + string.digits, k=5))
        r = request(s, "POST", f"{base}/xem-hoa-don", headers={"Referer": page, "Origin": base}, data=data)
        text = r.text or ""
        g = _STORE_GUID_RE.search(text)
        dl = _STORE_DL_RE.search(text)
        if g:                                 # MM Mega Market: link tải thẳng theo guid
            d = request(s, "GET", f"{base}/tai-ve-hoa-don/{g.group(1)}", headers={"Referer": r.url or page},
                        timeout=60)
        elif dl:                              # bản mới: form POST /Download/DownloadFile
            fields = {k: html.unescape(v) for k, v in _HIDDEN_RE.findall(dl.group(2))}
            if not fields.get("idHoaDon"):
                raise PdfFetchError(f"Thái Sơn ({base}): form tải HĐ không có idHoaDon", "bad_content")
            d = request(s, "POST", base + dl.group(1), headers={"Referer": r.url or page, "Origin": base},
                        data=fields, timeout=60)
        else:
            m = _STORE_ERR_RE.search(text)
            msg = html.unescape(m.group(1)).strip() if m else ""
            low = msg.lower()
            if "bảo mật" in low or "mã kiểm tra" in low or "xác thực" in low:
                raise _WrongCaptcha(msg)
            if "không tìm thấy" in low or "mã nhận" in low:
                raise _NotFound(msg)
            raise PdfFetchError(f"Thái Sơn ({base}) trả kết quả lạ (HTTP {r.status_code}): {msg}"[:300], "other")
        return _pdf_from_zip(d.content, base)


register(ThaisonAdapter())
