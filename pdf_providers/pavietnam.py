"""
P.A Việt Nam (hoadon30s) — MSTTCGP 0302431595. Chỉ HĐ do chính P.A Việt Nam bán (nbmst 0302431595).

HĐ của P.A KHÔNG nằm ở cổng chung tracuu.hoadon30s.vn (tra đúng mã ở đó trả 404 "Không tìm
thấy", cả khi tải file XML lên) mà ở cổng riêng https://hoadon.pavietnam.vn (PDF của P.A ghi
"Trang tra cứu: https://hoadon.pavietnam.vn  Mã tra cứu: ...").

Mã tra cứu: 13–14 chữ HOA (vd BLJDKOIDNZOYIP). **XML KHÔNG có mã này** (TTKhac chỉ có SDHang,
TTHCQNNuoc, Total8...), chỉ ghi trong PDF/mail → HĐ chỉ có XML từ Thuế thì 'no_code'.
(mail_reader.extract_codes hiện bỏ mã toàn chữ cái → mã P.A chưa lọt vào email_links.code.)

Quy trình (khảo sát + kiểm trên HĐ thật 10/2026):
  1. GET  /                     → cookie phiên (+ input token, đang để trống)
  2. GET  /captcha.php          → PNG 95x32, 6 ký tự HEX HOA (0-9A-F), chữ đỏ trên nền xám chấm trắng.
       Server KIỂM captcha thật (sai → toastr "Mã bảo vệ không chính xác").
       ddddocr ảnh thô: server nhận 8/20. Lọc chữ đỏ + phóng 3x + đóng lỗ (Min/Max filter) rồi
       thu lại cỡ gốc: 40/40.
  3. POST /lookup.php  form {thongtu=78, ckbtype=text, Username='', OrderID=<mã>, SecurityCode, token}
       Username ("Mã số khách / Email") không bắt buộc; bỏ trống OrderID thì bị đòi mã.
       → bảng kết quả: "Số hóa đơn" (8 số, vd 00088744) + link
         https://bill.hoadon30s.vn/lookup/<hash 40 hex>/pdf-download  (hash đổi mỗi lần tra)
       → không thấy: toastr "Không tìm thấy kết quả hóa đơn..."
  4. GET link pdf-download → application/pdf.
Cổng có tường lửa SafeLine: phiên requests dùng cipher cũ (common.new_session, SECLEVEL=0) bị
chặn 403 ngay từ trang chủ → dùng phiên requests mặc định.
"""
from __future__ import annotations

import html
import io
import re

import requests

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import BROWSER_HEADERS, check_pdf_is_invoice, is_pdf, link_params, request
from .ocr_ddddocr import ocr_text

BASE = "https://hoadon.pavietnam.vn/"
SELLERS = ("0302431595",)               # người bán có HĐ ở cổng này (chính P.A Việt Nam)
CAPTCHA_TRIES = 4
CAPTCHA_LEN = 6
_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,30}$")
_TOKEN_RE = re.compile(r'name="token" id="token" type="hidden" value="([^"]*)"')
_TOAST_RE = re.compile(r"toastr\.error\('([^']*)'\)")
_ROW_RE = re.compile(r'data-label="Số hóa đơn"[^>]*>\s*([^<]*?)\s*</td>.*?href="([^"]+/pdf-download)"', re.S)


class _WrongCaptcha(Exception):
    pass


def new_plain_session() -> requests.Session:
    """Phiên TLS mặc định — SafeLine chặn phiên cipher cũ của common.new_session."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def clean_captcha(img: bytes) -> bytes:
    """Giữ chữ đỏ, lấp các chấm trắng cắt ngang nét (phóng 3x → Min/Max filter → thu về cỡ gốc)."""
    from PIL import Image, ImageFilter
    im = Image.open(io.BytesIO(img)).convert("RGB")
    w, h = im.size
    out = Image.new("L", (w, h), 255)
    src, dst = im.load(), out.load()
    for x in range(w):
        for y in range(h):
            r, g, b = src[x, y]
            if r > 150 and g < 120 and b < 120:
                dst[x, y] = 0
    out = (out.resize((w * 3, h * 3), Image.NEAREST)
              .filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(3))
              .resize((w, h), Image.BILINEAR))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def solve(img: bytes) -> str:
    """6 ký tự hex HOA, hoặc _WrongCaptcha nếu đọc lệch."""
    try:
        png = clean_captcha(img)
    except Exception as e:  # noqa: BLE001
        raise _WrongCaptcha(f"ảnh captcha không đọc được: {e}") from e
    raw = ocr_text(png).upper().replace("O", "0").replace("I", "1").replace("L", "1")
    text = re.sub(r"[^0-9A-F]", "", raw)
    if len(text) != CAPTCHA_LEN:
        raise _WrongCaptcha(f"ddddocr đọc {raw!r}, captcha P.A là {CAPTCHA_LEN} ký tự hex")
    return text


class PaVietnamAdapter(PdfAdapter):
    source_id = "pavietnam"
    name = "P.A Việt Nam (hoadon30s)"
    mst_tcgp = ("0302431595",)
    hosts = ("hoadon.pavietnam.vn",)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Mail P.A: link trang chủ hoadon.pavietnam.vn + 'Mã tra cứu'. Không mở link."""
        p = link_params(url)
        secret = (code or p.get("orderid") or p.get("matracuu") or "").strip()
        if not _CODE_RE.match(secret):
            return None
        return {"ma_tra_cuu": secret, "tt_khac": {"MaTraCuu": secret}}

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for k, v in tt_khac.items():
            if k.lower().replace(" ", "") in ("matracuu", "mãtracứu") and str(v or "").strip():
                return str(v).strip()
        return str(invoice.get("ma_tra_cuu") or "").strip()

    def fetch_pdf(self, invoice: dict) -> bytes:
        seller = str(invoice.get("nbmst") or "").strip()
        if seller and seller not in SELLERS:
            raise PdfFetchError(
                f"Người bán {seller} dùng phần mềm hoadon30s nhưng chưa kiểm cổng tra cứu "
                f"(cổng chung tracuu.hoadon30s.vn chưa thử trên HĐ thật)", "no_portal")
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("Không có mã tra cứu P.A (XML không ghi, mã chỉ có trong PDF/mail)",
                                "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu P.A không hợp lệ: {code!r}", "no_code")
        for attempt in range(1, CAPTCHA_TRIES + 1):
            s = new_plain_session()
            try:
                rows = self._search(s, code)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"P.A Việt Nam từ chối/không đọc được captcha {CAPTCHA_TRIES} lần",
                                        "captcha")
        link = self._pick(rows, invoice, code)
        r = request(s, "GET", link, headers={"Referer": BASE})
        if r.status_code != 200 or not is_pdf(r.content):
            raise PdfFetchError(
                f"P.A trả nội dung không phải PDF ({r.headers.get('Content-Type')}, HTTP {r.status_code})",
                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    @staticmethod
    def _check_waf(r) -> None:
        if r.status_code == 403:
            raise PdfFetchError("hoadon.pavietnam.vn chặn 403 (tường lửa SafeLine)", "server")

    def _search(self, s, code: str) -> list:
        """→ [(số HĐ, link pdf-download)] của trang kết quả."""
        home = request(s, "GET", BASE)
        self._check_waf(home)
        token = (_TOKEN_RE.search(home.text) or [None, ""])[1]
        cap = request(s, "GET", BASE + "captcha.php", headers={"Referer": BASE})
        self._check_waf(cap)
        answer = solve(cap.content)
        r = request(s, "POST", BASE + "lookup.php", headers={"Referer": BASE},
                    data={"thongtu": "78", "ckbtype": "text", "Username": "", "OrderID": code,
                          "SecurityCode": answer, "token": token, "OrderLookup": ""})
        self._check_waf(r)
        errors = " | ".join(html.unescape(x) for x in _TOAST_RE.findall(r.text))
        if "bảo vệ" in errors.lower():
            raise _WrongCaptcha(errors)
        if "không tìm thấy" in errors.lower():
            raise PdfFetchError(f"P.A không tìm thấy HĐ với mã {code}", "no_pdf")
        rows = [(no.strip(), html.unescape(link)) for no, link in _ROW_RE.findall(r.text)]
        if not rows:
            raise PdfFetchError(f"Trang kết quả P.A không có link PDF ({errors or 'không có thông báo'})",
                                "bad_content")
        return rows

    @staticmethod
    def _pick(rows: list, invoice: dict, code: str) -> str:
        shdon = str(invoice.get("shdon") or "").strip()
        if not shdon:
            return rows[0][1]
        for no, link in rows:
            if no.lstrip("0") == shdon.lstrip("0"):
                return link
        raise PdfFetchError(f"Mã {code} trên cổng P.A trỏ tới HĐ số {[n for n, _ in rows]}, "
                            f"không phải {shdon}", "no_pdf")


register(PaVietnamAdapter())
