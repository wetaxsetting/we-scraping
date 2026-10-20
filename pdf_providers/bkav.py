# pdf_providers/bkav.py
"""
BKAV eHoadon — MSTTCGP 0101360697. Tra cứu: https://van.ehoadon.vn/TCHD

XML của BKAV KHÔNG có mã tra cứu (TTKhac rỗng), nhưng thuộc tính Id của DLHDon chính là
InvoiceGUID của BKAV (GUID có gạch, vd 70611f78-501c-4fce-a2dc-b31dd272f873). Trang xem
hóa đơn của cổng mở thẳng bằng GUID đó — không captcha, không cần MTC:

  1. GET /Lookup?InvoiceGUID=<guid>&ViewATF=     (trang WebForms, form#formMain)
       → không thấy: bkav_alert_error('Hóa đơn không tồn tại trên hệ thống …')
                     bkav_alert_error('InvoiceGUID không hợp lệ …')
  2. Tải PDF (cùng phiên) — tùy mẫu hóa đơn, trang có 1 trong 2 kiểu nút:
     a. DownloadFile('<FilePath>',<BFType>)  → GET /DownloadFile?FilePath&BFType (PDF lưu sẵn)
     b. __doPostBack('lbtDownloadAsPdf','')   → POST lại form với __EVENTTARGET=lbtDownloadAsPdf
        (mẫu xem dạng HTML; server tự dựng PDF)
       → application/pdf

Cần khóa invoice["dlhdon_id"] (= DLHDon/@Id) — xml_parser/invoice_from_xml phải đưa ra.
Tra bằng MTC trên /TCHD (có reCAPTCHA ẩn) hay upload XML lấy từ cổng Thuế ("File XML
không đúng cấu trúc hoặc đã bị sửa") đều không dùng được.

Khảo sát 10/2026: kiểu (a) C26TGN-609/610, C26MBB-17535972; kiểu (b) C26TAH-256, C26MDB-878.
"""
from __future__ import annotations

import html
import re

from . import register
from .base import PdfAdapter, PdfFetchError
from .common import check_pdf_is_invoice, is_pdf, new_session, request

BASE = "https://van.ehoadon.vn"
POSTBACK_PDF = "lbtDownloadAsPdf"

_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DOWNLOAD_RE = re.compile(r"DownloadFile\(&#39;([^&']+\.pdf)&#39;,\s*(\d+)\)", re.I)
_POSTBACK_RE = re.compile(r"__doPostBack\(&#39;" + POSTBACK_PDF + r"&#39;")
_ALERT_RE = re.compile(r"bkav_alert_\w+\('([^']*)'")
_INPUT_RE = re.compile(r"<input[^>]*>", re.I)

_MSG_NOT_FOUND = ("không tồn tại", "không hợp lệ", "không tìm thấy")


def _form_fields(page: str) -> dict:
    """Các ô input của form WebForms (hidden + text), như trình duyệt gửi khi postback."""
    out = {}
    for m in _INPUT_RE.finditer(page):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        if not name or re.search(r'type="(submit|image|button|file|checkbox|radio)"', tag, re.I):
            continue
        value = re.search(r'value="([^"]*)"', tag)
        out[html.unescape(name.group(1))] = html.unescape(value.group(1)) if value else ""
    return out


class BkavAdapter(PdfAdapter):
    source_id = "bkav"
    name = "BKAV eHoadon"
    mst_tcgp = ("0101360697",)

    @staticmethod
    def invoice_guid(invoice: dict) -> str:
        tt_khac = invoice.get("tt_khac") or {}
        for v in (invoice.get("dlhdon_id"), tt_khac.get("InvoiceGUID")):
            v = str(v or "").strip()
            if _GUID_RE.match(v):
                return v
        return ""

    def fetch_pdf(self, invoice: dict) -> bytes:
        guid = self.invoice_guid(invoice)
        if not guid:
            raise PdfFetchError("XML không có InvoiceGUID BKAV (DLHDon/@Id dạng GUID)", "no_code")
        s = new_session()
        lookup = f"{BASE}/Lookup"
        r = request(s, "GET", lookup, params={"InvoiceGUID": guid, "ViewATF": ""},
                    headers={"Referer": f"{BASE}/TCHD"})
        if r.status_code != 200:
            raise PdfFetchError(f"BKAV trả HTTP {r.status_code}", "other")
        page = r.text
        page_url = getattr(r, "url", "") or f"{lookup}?InvoiceGUID={guid}&ViewATF="

        if m := _DOWNLOAD_RE.search(page):                       # (a) PDF lưu sẵn
            d = request(s, "GET", f"{BASE}/DownloadFile",
                        params={"FilePath": html.unescape(m.group(1)), "BFType": m.group(2)},
                        headers={"Referer": page_url})
        elif _POSTBACK_RE.search(page):                          # (b) server dựng PDF
            fields = _form_fields(page)
            fields.update({"__EVENTTARGET": POSTBACK_PDF, "__EVENTARGUMENT": ""})
            d = request(s, "POST", page_url, data=fields, headers={"Referer": page_url, "Origin": BASE})
        else:
            alert = html.unescape(a.group(1)) if (a := _ALERT_RE.search(page)) else ""
            alert = alert.split("[!|")[0].strip()
            if not alert or any(k in alert.lower() for k in _MSG_NOT_FOUND):
                raise PdfFetchError(f"BKAV không tìm thấy HĐ (GUID {guid}): {alert or 'không có nút tải PDF'}",
                                    "no_pdf")
            raise PdfFetchError(f"BKAV: {alert}", "other")

        if d.status_code != 200:
            raise PdfFetchError(f"BKAV tải PDF trả HTTP {d.status_code}", "no_pdf")
        if not is_pdf(d.content):
            raise PdfFetchError(
                f"BKAV trả nội dung không phải PDF ({d.headers.get('Content-Type')})", "bad_content")
        check_pdf_is_invoice(d.content, invoice)
        return d.content


register(BkavAdapter())
