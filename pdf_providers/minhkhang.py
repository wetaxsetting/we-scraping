# pdf_providers/minhkhang.py
"""
Minh Khang Group (hoadondientuvat.com) — MSTTCGP 0314209362.
Tra cứu: https://hoadondientuvat.com/Tracuu.aspx  (1 ô "Mã hóa đơn", không captcha).

Cùng phần mềm với Vĩnh Hy (ehoadondientu.com) — xem vinhhy.py — nhưng máy chủ + CSDL riêng:
HĐ của Minh Khang chỉ tải được từ hoadondientuvat.com (cùng tham số r, ehoadondientu.com trả rỗng).
Khảo sát 23/10/2026 trên HĐ thật C24TLV-23 (người bán 0316994446): DLHDon/@Id
"031699444620220300000675" → GenerateFile.aspx?r=ct_03169944462022_0300000675&type=pdf → PDF
(PDF in "Mã hóa đơn: NWD2S2F0N5"; tra mã đó qua /Tracuu.aspx/tracuu ra đúng r trên).
"""
from __future__ import annotations

from . import register
from .vinhhy import VinhHyAdapter


class MinhKhangAdapter(VinhHyAdapter):
    source_id = "minhkhang"
    name = "Minh Khang (hoadondientuvat.com)"
    mst_tcgp = ("0314209362",)
    hosts = ("hoadondientuvat.com",)
    BASE = "https://hoadondientuvat.com"
    LOOKUP_PAGE = "/Tracuu.aspx"


register(MinhKhangAdapter())
