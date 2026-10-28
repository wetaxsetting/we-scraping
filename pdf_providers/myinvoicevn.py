"""
My-Invoice (Công ty TNHH Giải pháp hóa đơn điện tử My – Invoice, hoadondientuvn.info) — MSTTCGP 0106820789.
Tra cứu: https://tracuu.hoadondientuvn.info/#/tracuuhoadon/tracuu  (SPA xcyber)
KHÁC adapter `myinvoice` (My Software 0108971656).

Khảo sát 14/10/2026 (docs/Danh_sach_NCC.xlsx): trang tra cứu gọi thẳng API của CyberLotus
https://bill2app.xcyber.vn/api/services/hddt/TraCuuHoaDon/RefreshCaptcha (ảnh 300x75) — tức My-Invoice
là nhà bán lại dùng chung hệ thống 2 của CyberLotus, không có API riêng. Adapter vì vậy chỉ tra
bill2app (xem xcyber.py), Origin/Referer để theo trang của My-Invoice.

27/10/2026: tracuu.hoadondientuvn.info (125.212.234.84) từ chối kết nối cổng 443/80 (ECONNREFUSED / timeout),
không đọc lại được appconfig.json; trang chủ hoadondientuvn.info vẫn sống và vẫn trỏ link tra cứu về đó.
API bill2app vẫn hoạt động (adapter xcyber). Chưa có HĐ thật — chưa kiểm đường thành công.
"""
from __future__ import annotations

from . import register
from .xcyber import XcyberAdapter


class MyInvoiceVnAdapter(XcyberAdapter):
    source_id = "myinvoicevn"
    name = "My-Invoice (hoadondientuvn.info)"
    mst_tcgp = ("0106820789",)
    hosts = ("hoadondientuvn.info",)
    SYSTEMS = (("https://bill2app.xcyber.vn", "https://tracuu.hoadondientuvn.info"),)


register(MyInvoiceVnAdapter())
