"""
Giải pháp ATOM / 1invoice — MSTTCGP 0316887099. Tra cứu: https://tracuuhoadon.1invoice.vn/tra-cuu-hoa-don

Cùng phần mềm tra cứu M-invoice white-label với EPAY (xem epay.py): cùng bundle SPA, cùng API
/api/Search/SearchInvoice + /api/Search/DownloadXml (masothue + sobaomat), không captcha.
Khảo sát 27/10/2026: mã bịa → 500 {"code":"5000","message":"Get Tenant fail because not found
Tenant with code = [...]"} (MST chưa là khách 1invoice) / {"code":"5000","message":"Có lỗi xảy ra"}.
Chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy.
"""
from __future__ import annotations

from . import register
from .epay import EpayAdapter


class AtomAdapter(EpayAdapter):
    source_id = "atom"
    name = "ATOM 1invoice (tracuuhoadon.1invoice.vn)"
    mst_tcgp = ("0316887099",)
    hosts = ("tracuuhoadon.1invoice.vn",)
    BASE = "https://tracuuhoadon.1invoice.vn"


register(AtomAdapter())
