"""
Minh Thư Vũng Tàu — MSTTCGP 3500456910.
Tra cứu: https://hoadonminhthuvungtau.com/Tra-cuu  (1 ô "Mã hóa đơn", không captcha). Trang có tiêu đề "Hóa đơn Minh Thư Vũng Tàu".

Cùng phần mềm ASP.NET WebForms với Vĩnh Hy (ehoadondientu.com) — xem vinhhy.py — nhưng máy chủ
+ CSDL riêng, nên HĐ do nhà này phát hành chỉ tải được từ hoadonminhthuvungtau.com.
Khảo sát 27/10/2026 bằng request thật (chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy):
    POST /Tracuu.aspx/tracuu {mahoadon:<mã bịa>, loai:"mhd", lang:"Vn"} → 200 {"d": ""}
    GET  /Account/GenerateFile.aspx?r=ct_<14 số>_<10 số>&type=pdf (r bịa) → 200 text/html, thân rỗng
đúng dạng "không tìm thấy" của vinhhy. Đường thành công (PDF/XML) chưa thấy thật trên host này.
"""
from __future__ import annotations

from . import register
from .vinhhy import VinhHyAdapter


class MinhThuAdapter(VinhHyAdapter):
    source_id = "minhthu"
    name = "Minh Thư Vũng Tàu (hoadonminhthuvungtau.com)"
    mst_tcgp = ("3500456910",)
    hosts = ("hoadonminhthuvungtau.com",)
    BASE = "https://hoadonminhthuvungtau.com"
    LOOKUP_PAGE = "/Tra-cuu"


register(MinhThuAdapter())
