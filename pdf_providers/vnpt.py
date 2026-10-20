# pdf_providers/vnpt.py
"""
VNPT Invoice — MSTTCGP 0100684378.

Khác MISA/EasyInvoice ở 2 điểm (khảo sát 10/2026 trên HĐ thật K26TAN-2296483):

1. KHÔNG có PDF trên server. Nút "Tải file pdf" của cổng chạy html2pdf.js trên trình
   duyệt từ HTML xem trước → ta lấy cùng HTML đó và dựng PDF bằng Chromium (render.py).
   Bản gốc có chữ ký số chỉ là XML (đã có từ GDT).
2. Mỗi khách hàng lớn có CỔNG RIÊNG, không suy ra được từ MST người bán, và XML không
   ghi địa chỉ cổng (chỉ in trên hóa đơn: "Website tra cứu: ..."). → bảng SELLER_PORTALS.
   Người bán chưa có trong bảng → lỗi 'no_portal': HĐ nằm chờ, không tốn lượt thử, chạy
   lại khi bổ sung cổng (requeue --status no_source --mst-tcgp 0100684378).

Quy trình cổng dạng "SearchInvoicebycode" (đã kiểm: hoadon.petrolimex.com.vn):
  1. GET  /                      → __RequestVerificationToken trong form#SearchformByfkey
  2. GET  /Captcha/Show          → PNG 80x30, 4 chữ số, gắn cookie phiên → model g_80x30
  3. POST /SearchInvoicebycode/Index   token, tab=content1, strFkey (GIỮ dấu * cuối), captch
       → lỗi trong .errorbox; thành công: checkCode trong ajxCall4Portal...('<checkCode>')
  4. POST /SearchInvoicebycode/ajxPreview/  checkCode   → JSON {"str": HTML tự chứa}
  5. HTML → PDF (Chromium, tắt JS, chặn mạng)

Cổng chung HST (portal-hst.vnpt-invoice.com.vn, API gateway-hst) — dùng cho người bán
KHÔNG có trong SELLER_PORTALS. Chỉ chứa HĐ phát hành trên nền HST của VNPT (KHÔNG có HĐ
Petrolimex — khách hàng lớn chạy hệ thống VNPT riêng). Captcha trên trang chỉ kiểm ở trình
duyệt, API không đòi → không cần giải. Server tự trả PDF.
  GET  {api}/portal/key?key=<Fkey | MCCQT>          → content{id, fKey, ...} | err_code=1 (không thấy)
  POST {api}/portal/download-pdf  {ids:[id], fkey}  → content.data = PDF base64
       dự phòng: download-htmlv2 → content.view, rồi html-to-pdf {fkey, htmlView} → PDF thô
  {api} = /invoice/api/invoice (HĐ thường) | /pos/api/posinvoice (HĐ máy tính tiền, mã viết HOA;
          tra theo MCCQT: POST portal/find-by-mccqt {mccqt, totalPay})
  ⚠ Tra cứu đã kiểm (phản hồi "không thấy"); bước tải PDF đọc từ JS của cổng, CHƯA chạy với
    HĐ HST thật — PDF luôn được kiểm có đúng số HĐ nên sai cũng không lưu nhầm.
Người bán không có trong SELLER_PORTALS và HST cũng không thấy → 'no_portal' (nằm chờ).

Cổng dạng "<alias>-tt78.vnpt-invoice.com.vn" (đã kiểm 10/2026: vttphcm-tt78 = VNPT TP.HCM,
HĐ K26THA-6471438). Cùng phần mềm, cùng captcha với Petrolimex nhưng form khác và server CÓ PDF:
  1. GET  /                      → __RequestVerificationToken trong form#Searchform
  2. GET  /Captcha/Show          → PNG 80x30, 4 chữ số → g_80x30 (thử thật 13/13 đúng)
  3. POST /HomeNoLogin/SearchByFkey   token, isHomepage=true, strFkey, captch
       → captcha sai: về trang chủ, .errorbox "Mã xác thực không chính xác";
         thấy HĐ: checkCode trong ajxCall4Portal('<checkCode>', ...) (cố định theo HĐ);
         ⚠ KHÔNG thấy HĐ: server TREO, không trả lời (đã thử 60–120 s) → hết giờ = 'no_pdf'
  4. GET  /HomeNoLogin/downloadPDF?checkCode=&fkey=   → PDF (không cần cookie)
     GET  /Invoice/Download?checkCode=&fkey=&nameCus= → XML có chữ ký (kèm XSLT) → fetch_xml
Fkey của VNPT nằm ở thẻ <HDon><Fkey> (ngoài DLHDon, xml_parser chưa đọc) → nếu dict HĐ không có
mã thì đọc lại file XML theo xml_path (dict từ DB). Mail "Thông báo phát hành HDDT" ghi
"Mã tra cứu hóa đơn: ..." → invoice_from_link.

Chưa làm: cổng tracuuhoadon.vnpt-invoice.com.vn.
"""
from __future__ import annotations

import base64
import binascii
import html
import os
import re
import unicodedata
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import (ROOT, check_pdf_is_invoice, is_pdf, link_params, new_session, request,
                     solve_captcha)

CAPTCHA_GROUP = "g_80x30"
CAPTCHA_TRIES = 3

# Cổng dạng SearchInvoicebycode (white-label VNPT cho khách hàng lớn)
PORTALS = {
    "petrolimex": "https://hoadon.petrolimex.com.vn",
    "vttphcm": "https://vttphcm-tt78.vnpt-invoice.com.vn",     # VNPT TP.HCM (cước viễn thông) — dạng -tt78
}

# Người bán → cổng. Khớp theo MST (chính xác) hoặc tên người bán (chứa chuỗi, không phân biệt hoa thường).
# Bổ sung từ dòng "Website tra cứu" in trên hóa đơn của người bán đó.
SELLER_PORTALS = [
    {"portal": "petrolimex", "mst": {"0300555450"}, "name_contains": "PETROLIMEX"},
    {"portal": "vttphcm", "mst": {"0300954529"}},             # Viễn thông TP.HCM
]

_TOKEN_RE = r'id="{form_id}".*?name="__RequestVerificationToken" type="hidden" value="([^"]+)"'
_ERRORBOX_RE = re.compile(r'class="errorbox">.*?/>\s*(.*?)<', re.S)
_CHECKCODE_RE = re.compile(r"ajxCall4Portal\w*\('([^']+)'")

# Cổng mẫu TT78 riêng của từng khách hàng VNPT
_TT78_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-tt78\.vnpt-invoice\.com\.vn$")
TT78_SEARCH_TIMEOUT = 45          # HĐ có: < 1 s; không có: server treo → hết giờ = không thấy
_CODE_CACHE_MAX = 200
# "Mã tra cứu hóa đơn: HCM10013369202609" (mail "Thông báo phát hành HDDT")
_MAIL_CODE_RE = re.compile(r"(?:m[ãa]\s*tra\s*c[ứu]u(?:\s*h[óo]a\s*[đd][ơo]n)?|fkey)\s*[:：]\s*"
                           r"([A-Za-z0-9][A-Za-z0-9_\-\*\.]{4,59})", re.I)
# "Hóa đơn của khách hàng có số 06471438 thuộc mẫu 1/023 và serial K26THA"
_MAIL_INV_RE = re.compile(r"s[ốo]\s+0*(\d+)\s+thu[ộo]c\s+m[ẫa]u\s+(\d)/\S*\s+v[àa]\s+serial\s+([A-Z0-9]{6})", re.I)
_BARE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\*\.]{4,59}$")


def _is_tt78(base: str | None) -> bool:
    return bool(base) and bool(_TT78_HOST_RE.match((urlsplit(base).hostname or "").lower()))


def _root_fkey(invoice: dict) -> str:
    """Fkey VNPT ở <HDon><Fkey> (ngoài DLHDon, xml_parser không đọc) → đọc lại file XML đã lưu."""
    path = invoice.get("xml_path")
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute():               # DB lưu đường dẫn tương đối theo INVOICE_OUTPUT_DIR
        p = Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / p
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    m = re.search(rb"<Fkey>\s*([^<\s]+)\s*</Fkey>", data)
    return m.group(1).decode("utf-8", "replace") if m else ""

HST_GATEWAY = "https://gateway-hst.vnpt-invoice.com.vn"
HST_PORTAL = "https://portal-hst.vnpt-invoice.com.vn"
_HST_HEADERS = {"app": "portal", "Accept": "application/json, text/plain, */*",
                "Origin": HST_PORTAL, "Referer": f"{HST_PORTAL}/"}

_MSG_CAPTCHA = ("mã xác thực không chính xác", "vui lòng nhập mã xác thực", "mã xác nhận không đúng")
_MSG_NOT_FOUND = ("không tìm thấy hóa đơn", "không tồn tại")


def _norm(s: str) -> str:
    # Các cổng viết "hóa"/"hoá" khác nhau → chuẩn hóa NFC + bỏ dấu thanh vị trí khác
    s = unicodedata.normalize("NFC", html.unescape(s)).lower()
    return s.replace("hoá", "hóa")


class _WrongCaptcha(Exception):
    pass


class VnptAdapter(PdfAdapter):
    source_id = "vnpt"
    name = "VNPT Invoice"
    mst_tcgp = ("0100684378",)
    hosts = ("vnpt-invoice.com.vn",)      # link mail: chỉ nhận cổng <alias>-tt78 (invoice_from_link)

    def __init__(self):
        self._codes: dict = {}            # (cổng, Fkey) → checkCode: fetch_xml + fetch_pdf chỉ giải 1 captcha

    @staticmethod
    def portal_for(invoice: dict) -> str | None:
        mst = (invoice.get("nbmst") or "").strip()
        name = (invoice.get("nbten") or "").upper()
        for rule in SELLER_PORTALS:
            if mst in rule.get("mst", ()) or (rule.get("name_contains") and rule["name_contains"] in name):
                return PORTALS[rule["portal"]]
        url = (invoice.get("lookup_url") or "").strip()   # link -tt78 lấy từ mail
        if _is_tt78(url):
            return f"https://{urlsplit(url).hostname.lower()}"
        return None

    @staticmethod
    def fkey(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        key = (tt_khac.get("Fkey") or tt_khac.get("FKey") or invoice.get("ma_tra_cuu") or "").strip()
        return key or _root_fkey(invoice)

    def invoice_from_link(self, url: str, code, mail_text: str):
        """
        Mail VNPT: link trang chủ cổng <alias>-tt78.vnpt-invoice.com.vn + "Mã tra cứu hóa đơn: <Fkey>"
        (+ "có số 06471438 thuộc mẫu 1/023 và serial K26THA"). Chỉ đọc chuỗi, không mở link.
        Cổng VNPT khác (HST, tracuuhoadon...) hoặc không có mã → None.
        """
        host = (urlsplit(url).hostname or "").lower()
        if not _TT78_HOST_RE.match(host):
            return None
        base = f"https://{host}"
        params = link_params(url)
        key = (code or params.get("fkey") or params.get("strfkey") or "").strip()
        if not key:
            m = _MAIL_CODE_RE.search(mail_text or "")
            if m:
                key = m.group(1).strip(".-")
            else:                             # process_links: dòng 1 = tiêu đề, sau đó mỗi dòng 1 mã
                bare = [ln.strip() for ln in (mail_text or "").splitlines()[1:]
                        if _BARE_CODE_RE.match(ln.strip()) and not ln.strip().isalpha()]
                key = bare[0] if len(bare) == 1 else ""
        if not key:
            return None
        inv = {"ma_tra_cuu": key, "tt_khac": {"Fkey": key}, "lookup_url": base}
        msts = [m for r in SELLER_PORTALS if PORTALS[r["portal"]] == base for m in r.get("mst", ())]
        if len(msts) == 1:
            inv["nbmst"] = msts[0]
        m = _MAIL_INV_RE.search(mail_text or "")
        if m:
            inv.update(shdon=int(m.group(1)), khmshdon=int(m.group(2)), khhdon=m.group(3).upper())
        return inv

    def fetch_pdf(self, invoice: dict) -> bytes:
        fkey = self.fkey(invoice)
        ma_cqt = (invoice.get("ma_cqt") or "").strip()
        if not fkey and not ma_cqt:
            raise PdfFetchError("XML không có Fkey/MCCQT (mã tra cứu VNPT)", "no_code")
        base = self.portal_for(invoice)
        if not base:
            pdf = self._hst_fetch(invoice, fkey, ma_cqt)
            if pdf is None:
                raise PdfFetchError(
                    f"Cổng chung VNPT HST không có HĐ của người bán {invoice.get('nbmst')} "
                    f"({invoice.get('nbten') or '?'}) — người bán dùng cổng riêng: bổ sung vào "
                    f"SELLER_PORTALS trong pdf_providers/vnpt.py (xem 'Website tra cứu' trên hóa đơn)",
                    "no_portal")
            return pdf
        if not fkey:
            raise PdfFetchError("Cổng riêng VNPT chỉ tra theo Fkey, XML không có Fkey", "no_code")
        if _is_tt78(base):
            return self._tt78_pdf(base, fkey, invoice)
        for attempt in range(1, CAPTCHA_TRIES + 1):
            try:
                check_code = self._search(base, fkey)
                break
            except _WrongCaptcha:
                if attempt == CAPTCHA_TRIES:
                    raise PdfFetchError(f"VNPT từ chối captcha {CAPTCHA_TRIES} lần", "captcha")
        return self._render(base, check_code, invoice)

    def _search(self, base: str, fkey: str) -> str:
        s = new_session()                       # captcha gắn cookie phiên
        page = request(s, "GET", f"{base}/")
        m = re.search(_TOKEN_RE.format(form_id="SearchformByfkey"), page.text, re.S)
        if not m:
            raise PdfFetchError(f"Không thấy form tra cứu theo Fkey trên {base}", "server")
        img = request(s, "GET", f"{base}/Captcha/Show", headers={"Referer": f"{base}/"})
        answer = solve_captcha(img.content, CAPTCHA_GROUP, min_conf=0.9)
        if not (len(answer) == 4 and answer.isdigit()):
            raise _WrongCaptcha(f"model đọc {answer!r}, captcha VNPT là 4 chữ số")   # không gửi bừa
        r = request(s, "POST", f"{base}/SearchInvoicebycode/Index",
                    data={"__RequestVerificationToken": m.group(1), "tab": "content1",
                          "strFkey": fkey, "captch": answer},
                    headers={"Referer": f"{base}/", "Origin": base})
        err = _ERRORBOX_RE.search(r.text)
        msg = _norm(err.group(1).strip()) if err else ""
        if any(k in msg for k in _MSG_CAPTCHA):
            raise _WrongCaptcha(msg)
        if any(k in msg for k in _MSG_NOT_FOUND):
            raise PdfFetchError(f"VNPT không tìm thấy HĐ với Fkey {fkey}", "no_pdf")
        codes = list(dict.fromkeys(_CHECKCODE_RE.findall(r.text)))
        if not codes:
            raise PdfFetchError(f"Trang kết quả VNPT không có checkCode ({msg or 'không có thông báo'})",
                                "bad_content")
        return codes[0]

    def _render(self, base: str, check_code: str, invoice: dict) -> bytes:
        from .render import html_to_pdf
        s = new_session()                       # checkCode dùng như token, không cần cookie
        r = request(s, "POST", f"{base}/SearchInvoicebycode/ajxPreview/",
                    data={"checkCode": check_code},
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Referer": f"{base}/SearchInvoicebycode/Index"})
        try:
            body = r.json()["str"]
        except (ValueError, KeyError, TypeError) as e:
            raise PdfFetchError(f"VNPT ajxPreview không trả HTML hóa đơn (HTTP {r.status_code})",
                                "bad_content") from e
        pdf = html_to_pdf(body, base=f"{base}/")
        if not is_pdf(pdf):
            raise PdfFetchError("Chromium dựng ra PDF rỗng", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    # ------------------------------------------------------------------
    # Cổng <alias>-tt78 (server có PDF và XML)
    # ------------------------------------------------------------------
    def fetch_xml(self, invoice: dict) -> bytes:
        """XML có chữ ký từ cổng -tt78 — có ích cho HĐ hthdon=2 (Thuế không có XML)."""
        base, fkey = self.portal_for(invoice), self.fkey(invoice)
        if not _is_tt78(base):
            raise PdfFetchError("Chỉ cổng VNPT -tt78 cho tải XML", "no_portal")
        if not fkey:
            raise PdfFetchError("Không có Fkey (mã tra cứu VNPT)", "no_code")
        r = request(new_session(), "GET", f"{base}/Invoice/Download",
                    params={"checkCode": self._tt78_check_code(base, fkey), "fkey": fkey, "nameCus": ""},
                    headers={"Referer": f"{base}/HomeNoLogin/SearchByFkey"})
        data = r.content or b""
        head = data[:20000]
        if b"<HDon" not in head or b"<DLHDon" not in head:
            raise PdfFetchError(f"VNPT {base} trả không phải XML hóa đơn (HTTP {r.status_code})", "bad_content")
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", head)
        shdon = str(invoice.get("shdon") or "").strip().lstrip("0")
        if shdon and (not m or m.group(1).decode() != shdon):
            raise PdfFetchError(f"XML tải về không phải HĐ số {shdon}", "bad_content")
        return data

    def _tt78_pdf(self, base: str, fkey: str, invoice: dict) -> bytes:
        r = request(new_session(), "GET", f"{base}/HomeNoLogin/downloadPDF",
                    params={"checkCode": self._tt78_check_code(base, fkey), "fkey": fkey},
                    headers={"Referer": f"{base}/HomeNoLogin/SearchByFkey"})
        if not is_pdf(r.content):
            raise PdfFetchError(f"VNPT {base} downloadPDF trả không phải PDF (HTTP {r.status_code})",
                                "bad_content")
        check_pdf_is_invoice(r.content, invoice)
        return r.content

    def _tt78_check_code(self, base: str, fkey: str) -> str:
        key = (base, fkey)
        if key not in self._codes:
            for attempt in range(1, CAPTCHA_TRIES + 1):
                try:
                    code = self._tt78_search(base, fkey)
                    break
                except _WrongCaptcha:
                    if attempt == CAPTCHA_TRIES:
                        raise PdfFetchError(f"VNPT từ chối captcha {CAPTCHA_TRIES} lần", "captcha")
            if len(self._codes) >= _CODE_CACHE_MAX:
                self._codes.clear()
            self._codes[key] = code
        return self._codes[key]

    def _tt78_search(self, base: str, fkey: str) -> str:
        s = new_session()                       # captcha gắn cookie phiên
        page = request(s, "GET", f"{base}/")
        m = re.search(_TOKEN_RE.format(form_id="Searchform"), page.text, re.S)
        if not m:
            raise PdfFetchError(f"Không thấy form tra cứu theo Fkey trên {base}", "server")
        img = request(s, "GET", f"{base}/Captcha/Show", headers={"Referer": f"{base}/"})
        answer = solve_captcha(img.content, CAPTCHA_GROUP, min_conf=0.9)
        if not (len(answer) == 4 and answer.isdigit()):
            raise _WrongCaptcha(f"model đọc {answer!r}, captcha VNPT là 4 chữ số")   # không gửi bừa
        try:
            r = request(s, "POST", f"{base}/HomeNoLogin/SearchByFkey",
                        data={"__RequestVerificationToken": m.group(1), "isHomepage": "true",
                              "strFkey": fkey, "captch": answer},
                        headers={"Referer": f"{base}/", "Origin": base}, timeout=TT78_SEARCH_TIMEOUT)
        except PdfFetchError as e:
            if e.kind == "network" and str(e).startswith("Timeout"):
                # Trang chủ + captcha vừa trả bình thường; cổng -tt78 treo khi Fkey không có HĐ
                raise PdfFetchError(f"VNPT {base} không trả lời với Fkey {fkey} "
                                    f"(cổng treo khi không tìm thấy HĐ)", "no_pdf") from e
            raise
        err = _ERRORBOX_RE.search(r.text)
        msg = _norm(err.group(1).strip()) if err else ""
        if any(k in msg for k in _MSG_CAPTCHA):
            raise _WrongCaptcha(msg)
        if any(k in msg for k in _MSG_NOT_FOUND):
            raise PdfFetchError(f"VNPT không tìm thấy HĐ với Fkey {fkey}", "no_pdf")
        codes = list(dict.fromkeys(_CHECKCODE_RE.findall(r.text)))
        if not codes:
            raise PdfFetchError(f"Trang kết quả VNPT không có checkCode ({msg or 'không có thông báo'})",
                                "bad_content")
        return html.unescape(codes[0])

    # ------------------------------------------------------------------
    # Cổng chung HST (không captcha phía server, server tự trả PDF)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_mtt(invoice: dict) -> bool:
        return str(invoice.get("sub_type", "")).endswith("-mtt") or invoice.get("loai_hd") == "MTT"

    def _hst_fetch(self, invoice: dict, fkey: str, ma_cqt: str):
        """PDF từ cổng HST, hoặc None nếu HST không có hóa đơn này."""
        mtt = self._is_mtt(invoice)
        api = HST_GATEWAY + ("/pos/api/posinvoice" if mtt else "/invoice/api/invoice")
        s = new_session()
        if fkey or not mtt:
            key = fkey or ma_cqt
            r = request(s, "GET", f"{api}/portal/key", headers=_HST_HEADERS,
                        params={"key": key.upper() if mtt else key})
        else:
            total = invoice.get("tong_tien")
            if isinstance(total, Decimal):
                total = int(total) if total == total.to_integral_value() else float(total)
            r = request(s, "POST", f"{api}/portal/find-by-mccqt", headers=_HST_HEADERS,
                        json={"mccqt": ma_cqt, "totalPay": total})
        found = self._hst_content(r)
        if found is None:
            return None
        body = {"ids": [found.get("id")], "fkey": found.get("fKey") or fkey}
        pdf = self._hst_pdf(s, api, body)
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    @staticmethod
    def _hst_content(r):
        try:
            js = r.json()
        except ValueError as e:
            raise PdfFetchError(f"VNPT HST trả không phải JSON (HTTP {r.status_code})", "bad_content") from e
        content = js.get("content") if isinstance(js, dict) else None
        if str(js.get("err_code")) != "0" or not content:
            return None                                    # "Không tìm thấy hóa đơn"
        if isinstance(content, list):
            content = content[0]
        return content if isinstance(content, dict) and content.get("id") is not None else None

    @staticmethod
    def _hst_pdf(s, api: str, body: dict) -> bytes:
        r = request(s, "POST", f"{api}/portal/download-pdf", headers=_HST_HEADERS, json=body)
        pdf = None
        try:
            data = (r.json().get("content") or {}).get("data")
            pdf = base64.b64decode(data) if data else None
        except (ValueError, AttributeError, binascii.Error):
            pdf = None
        if not is_pdf(pdf):                                # dự phòng: HTML → PDF ở server
            r1 = request(s, "POST", f"{api}/portal/download-htmlv2", headers=_HST_HEADERS, json=body)
            try:
                view = r1.json()["content"]["view"]
            except (ValueError, KeyError, TypeError) as e:
                raise PdfFetchError("VNPT HST không trả PDF lẫn HTML hóa đơn", "bad_content") from e
            r2 = request(s, "POST", f"{api}/portal/html-to-pdf", headers=_HST_HEADERS,
                         json={"fkey": body["fkey"], "htmlView": view})
            pdf = r2.content
        if not is_pdf(pdf):
            raise PdfFetchError("VNPT HST trả nội dung không phải PDF", "bad_content")
        return pdf


register(VnptAdapter())
