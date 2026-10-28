"""
Tia Lửa Việt — MSTTCGP 0303609305.
Tra cứu: https://ihoadondientu.com/Tra-cuu  (1 ô "Mã hóa đơn", không captcha). Trang có tiêu đề "Hóa đơn điện tử Tia Lửa Việt".

Cùng phần mềm ASP.NET WebForms với Vĩnh Hy (ehoadondientu.com) — xem vinhhy.py — nhưng máy chủ
+ CSDL riêng, nên HĐ do nhà này phát hành chỉ tải được từ ihoadondientu.com.
Khảo sát 27/10/2026 bằng request thật (chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy):
    POST /Tracuu.aspx/tracuu {mahoadon:<mã bịa>, loai:"mhd", lang:"Vn"} → 200 {"d": ""}
    GET  /Account/GenerateFile.aspx?r=ct_<14 số>_<10 số>&type=pdf (r bịa) → 200 text/html, thân rỗng
đúng dạng "không tìm thấy" của vinhhy. Đường thành công (PDF/XML) chưa thấy thật trên host này.
"""
from __future__ import annotations

from . import register
from .vinhhy import VinhHyAdapter


class TiaLuaVietAdapter(VinhHyAdapter):
    source_id = "tialuaviet"
    name = "Tia Lửa Việt (ihoadondientu.com)"
    mst_tcgp = ("0303609305",)
    hosts = ("ihoadondientu.com",)
    BASE = "https://ihoadondientu.com"
    LOOKUP_PAGE = "/Tra-cuu"


register(TiaLuaVietAdapter())
