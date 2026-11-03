"""
VN-Invoice / vninvoice.vn — MSTTCGP 0101352495 (VNIS), 0103018807.
Tra cứu: https://tracuu.v50.vninvoice.vn  (Angular; API ở gateway.v50.vninvoice.vn)

Mã tra cứu: TTKhac "Mã bảo mật" (FamilyMart 0312283473: 20 ký tự như `2023110290FwB0JnUm05`,
Phong Vũ 0304998358: 7 chữ số). PDF in cùng mã với nhãn "Mã tra cứu" / "Số bảo mật".
Cổng KHÔNG nhận mã CQT hay DLHDon/@Id.

Quy trình (đọc từ bundle cổng + thử trên HĐ thật 02/11/2026):
  1. GET  /api/portal/PortalApiService/captcha
       → {image: data-URI PNG 180x50, key}. 6 ký tự, PHÂN BIỆT hoa/thường → model g_180x50_vn
       (server nhận 28/30; 2 lần trượt đều đọc ra 5 ký tự → đáp án ≠ 6 ký tự thì xin ảnh mới).
  2. POST /api/portal/PortalApiService/portal-public
       {transactionId, captcha, secret: key, taxCode: MST bán, fromDate, toDate (yyyy-mm-dd)}
       → 200 [ {id, templateNo, serialNo, invoiceNo "00278167", vnisType, sellerTaxCode,
                transactionId, ...} ]   (rỗng = không có HĐ)
       → 500 {"error": {"message": "Mã xác thực không đúng"}}   captcha sai
       Server kiểm captcha trước mọi trường khác.
  3. POST /api/portal/PortalApiService/download/public/unofficial  (body = phần tử bước 2)
       → {fileName, fileBytes: base64 PDF}. download/public/xml cùng body → XML có chữ ký.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import re

import requests

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, solve_captcha

API = "https://gateway.v50.vninvoice.vn/api/portal/PortalApiService/"
PORTAL = "https://tracuu.v50.vninvoice.vn/"
GROUP = "g_180x50_vn"
CAPTCHA_LEN = 6
MAX_TRIES = 6
TIMEOUT = 45
_CODE_KEYS = ("mãbảomật", "mãtracứu", "sốbảomật", "transactionid")
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,40}$")
_HEADERS = {"Origin": PORTAL.rstrip("/"), "Referer": PORTAL, "Accept": "application/json, text/plain, */*"}


def _norm(k: str) -> str:
    return re.sub(r"\s+", "", str(k or "")).lower()


def _call(s: requests.Session, method: str, path: str, **kw) -> requests.Response:
    """Như common.request nhưng KHÔNG coi HTTP 500 là lỗi server: cổng trả 500 cho captcha sai."""
    try:
        r = s.request(method, API + path, timeout=TIMEOUT, headers=_HEADERS, **kw)
    except requests.Timeout as e:
        raise PdfFetchError(f"Timeout vninvoice {path}: {e}", "network") from e
    except requests.RequestException as e:
        raise PdfFetchError(f"Lỗi kết nối vninvoice {path}: {e}", "network") from e
    if r.status_code == 429:
        raise PdfFetchError("vninvoice 429", "rate_limit")
    if r.status_code >= 502:
        raise PdfFetchError(f"vninvoice HTTP {r.status_code} ({path})", "server")
    return r


def _error_message(r: requests.Response) -> str:
    try:
        return str(((r.json() or {}).get("error") or {}).get("message") or "")
    except ValueError:
        return r.text[:200]


class VnInvoiceAdapter(PdfAdapter):
    source_id = "vninvoice"
    name = "VN-Invoice (vninvoice.vn)"
    mst_tcgp = ("0101352495", "0103018807")
    hosts = ("vninvoice.vn",)

    def __init__(self):
        self._last: tuple = ("", None)       # (khóa HĐ, (session, item)) — fetch_xml dùng lại lần tra

    @staticmethod
    def lookup_code(invoice: dict) -> str:
        for k, v in (invoice.get("tt_khac") or {}).items():
            if _norm(k) in _CODE_KEYS and str(v or "").strip():
                return str(v).strip()
        return str(invoice.get("ma_tra_cuu") or "").strip()

    @staticmethod
    def _day(invoice: dict) -> str:
        d = str(invoice.get("ngay_lap") or "")[:10]
        try:
            return dt.date.fromisoformat(d).isoformat()
        except ValueError as e:
            raise PdfFetchError(f"HĐ thiếu ngày lập hợp lệ ({d!r})", "no_code") from e

    def _find(self, invoice: dict) -> tuple:
        """(session, phần tử danh sách) của đúng HĐ này."""
        code = self.lookup_code(invoice)
        if not code:
            raise PdfFetchError("XML không có TTKhac 'Mã bảo mật' (mã tra cứu vninvoice)", "no_code")
        if not _CODE_RE.match(code):
            raise PdfFetchError(f"Mã tra cứu vninvoice không đúng dạng: {code!r}", "no_code")
        seller = str(invoice.get("nbmst") or "").strip()
        day = self._day(invoice)
        key = (seller, code, invoice.get("khhdon"), str(invoice.get("shdon")))
        if self._last[0] == key:
            return self._last[1]
        for _ in range(MAX_TRIES):
            s = new_session()
            j = _call(s, "GET", "captcha").json()
            try:
                img = base64.b64decode(str(j["image"]).split(",", 1)[1])
            except (KeyError, IndexError, binascii.Error) as e:
                raise PdfFetchError("vninvoice trả captcha không đúng dạng", "bad_content") from e
            try:
                cap = solve_captcha(img, GROUP)
            except PdfFetchError:
                continue                                   # model không chắc → ảnh mới
            if len(cap) != CAPTCHA_LEN:                    # lỗi đã biết: nuốt ký tự lặp
                continue
            r = _call(s, "POST", "portal-public", json={
                "transactionId": code, "captcha": cap, "secret": j.get("key"),
                "taxCode": seller, "fromDate": day, "toDate": day})
            if r.status_code == 200:
                items = r.json() or []
                want = int(invoice.get("shdon") or 0)
                for it in items:
                    if str(it.get("serialNo") or "").upper() == str(invoice.get("khhdon") or "").upper() \
                            and int(it.get("number") or it.get("invoiceNo") or 0) == want:
                        self._last = (key, (s, it))
                        return s, it
                raise PdfFetchError(
                    f"vninvoice không thấy HĐ {invoice.get('khhdon')}-{want} với mã {code} "
                    f"({len(items)} kết quả)", "no_pdf")
            msg = _error_message(r)
            if "xác thực" in msg.lower():
                continue                                   # captcha sai → ảnh mới
            if "không tìm thấy" in msg.lower():
                raise PdfFetchError(f"vninvoice: {msg}"[:300], "no_pdf")
            raise PdfFetchError(f"vninvoice HTTP {r.status_code}: {msg}"[:300], "other")
        raise PdfFetchError(f"vninvoice: đọc sai captcha {MAX_TRIES} lần liền", "captcha")

    def _download(self, invoice: dict, path: str) -> bytes:
        s, it = self._find(invoice)
        body = {k: it.get(k) for k in ("id", "templateNo", "serialNo", "invoiceNo", "vnisType",
                                       "sellerTaxCode", "transactionId")}
        r = _call(s, "POST", f"download/public/{path}", json=body)
        try:
            return base64.b64decode((r.json() or {}).get("fileBytes") or "")
        except (ValueError, binascii.Error) as e:
            raise PdfFetchError(f"vninvoice trả file không đúng dạng (HTTP {r.status_code})", "bad_content") from e

    def fetch_pdf(self, invoice: dict) -> bytes:
        pdf = self._download(invoice, "unofficial")
        if not is_pdf(pdf):
            raise PdfFetchError("vninvoice không trả PDF", "bad_content")
        check_pdf_is_invoice(pdf, invoice)
        return pdf

    def fetch_xml(self, invoice: dict) -> bytes:
        xml = self._download(invoice, "xml")
        m = re.search(rb"<SHDon>\s*0*(\d+)\s*</SHDon>", xml)
        if not m or int(m.group(1)) != int(invoice.get("shdon") or 0):
            raise PdfFetchError(f"XML vninvoice tải về không phải HĐ số {invoice.get('shdon')}", "bad_content")
        return xml


register(VnInvoiceAdapter())
