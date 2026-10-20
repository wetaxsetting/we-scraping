# pdf_providers/base.py
"""
Giao diện adapter tải PDF bản gốc từ site tra cứu của đơn vị cung cấp HĐĐT.

Mỗi đơn vị (VNPT, EasyInvoice, MISA...) có site tra cứu, cách nhập mã tra cứu và
captcha khác nhau → mỗi đơn vị 1 adapter. Adapter nhận dict hóa đơn gồm:
    nbmst, khmshdon, khhdon, shdon, ngay_lap, tong_tien ...   (bảng invoices)
    mst_tcgp, ma_tra_cuu, lookup_url, tt_khac                  (từ XML)
và trả bytes PDF, hoặc raise PdfFetchError với kind để hộp chờ biết chờ bao lâu.
"""
from __future__ import annotations


class PdfFetchError(Exception):
    """
    kind:
        network | server | rate_limit  site lỗi tạm thời
        captcha                        model đọc sai captcha
        no_pdf                         site chưa có / không tìm thấy HĐ
        bad_content | other            khác
    """

    def __init__(self, msg: str, kind: str = "other"):
        super().__init__(msg)
        self.kind = kind


class PdfAdapter:
    source_id: str = ""       # khớp einvoice_providers.source_id (vd "misa_meinvoice")
    name: str = ""
    mst_tcgp: tuple = ()      # MSTTCGP mà adapter tự nhận (không cần set-provider)
    hosts: tuple = ()         # tên miền cổng tra cứu (khớp cả subdomain) — để nhận link trong mail

    def fetch_pdf(self, invoice: dict) -> bytes:
        raise NotImplementedError

    def invoice_from_link(self, url: str, code: str | None, mail_text: str) -> dict | None:
        """
        Mail chỉ có link/mã (vd HĐ hthdon=2: Thuế không có XML) → dict HĐ cho fetch_pdf
        (khóa như web/fetch_pdf.invoice_from_xml: nbmst, ma_tra_cuu, tt_khac, ...).
        CHỈ đọc tham số trên URL / mã trong nội dung mail — KHÔNG mở URL (link trong mail có thể
        là link theo dõi / hủy nhận mail). None = adapter không dùng được link này.
        """
        return None
