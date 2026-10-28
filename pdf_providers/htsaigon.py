"""
HT Sài Gòn — MSTTCGP 0312942260.
Tra cứu: https://ihoadondientu.net/Tracuu.aspx  (1 ô "Mã hóa đơn", không captcha). Trang có tiêu đề "Hóa đơn điện tử".

Cùng phần mềm ASP.NET WebForms với Vĩnh Hy (ehoadondientu.com) — xem vinhhy.py — nhưng máy chủ
+ CSDL riêng, nên HĐ do nhà này phát hành chỉ tải được từ ihoadondientu.net.
Khảo sát 27/10/2026 bằng request thật (chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy):
    POST /Tracuu.aspx/tracuu {mahoadon:<mã bịa>, loai:"mhd", lang:"Vn"} → 200 {"d": ""}
    GET  /Account/GenerateFile.aspx?r=ct_<14 số>_<10 số>&type=pdf (r bịa) → 200 text/html, thân rỗng
đúng dạng "không tìm thấy" của vinhhy. Đường thành công (PDF/XML) chưa thấy thật trên host này.
"""
from __future__ import annotations

from . import register
from .vinhhy import VinhHyAdapter


class HtSaiGonAdapter(VinhHyAdapter):
    source_id = "htsaigon"
    name = "HT Sài Gòn (ihoadondientu.net)"
    mst_tcgp = ("0312942260",)
    hosts = ("ihoadondientu.net",)
    BASE = "https://ihoadondientu.net"
    LOOKUP_PAGE = "/Tracuu.aspx"


register(HtSaiGonAdapter())
