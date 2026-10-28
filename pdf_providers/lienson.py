"""
Liên Sơn (Công ty TNHH Giấy vi tính Liên Sơn) — MSTTCGP 0301452923.
Tra cứu: https://tracuu.lienson.vn/#/tracuuhoadon/tracuu  (SPA Angular, cùng mã nguồn xcyber)

Cùng nền tảng CyberLotus/xcyber — xem xcyber.py — nhưng API + CSDL riêng:
assets/appconfig.json ghi remoteServiceBaseUrl = https://ls-invoiceapp.lienson.vn, bundle main.js có đủ
/api/services/hddt/TraCuuHoaDon/{RefreshCaptcha,TraCuu,DownloadPdf} và /File/DownloadTempFile.
Chỉ 1 hệ thống (không có bản "dự phòng" như CyberLotus).

Khảo sát 27/10/2026 bằng request thật (chưa có HĐ thật — chỉ kiểm phản hồi không tìm thấy):
    POST RefreshCaptcha → ảnh PNG 300x75 RGBA, 5 chữ số → model g_300x75 đọc đúng (51891)
    POST TraCuu {key, captcha:<model>, maSoBiMat:<mã bịa 12 ký tự>} → status 4 (không thấy)
    POST TraCuu {key, captcha:"00000", ...}                         → status 2 "Mã xác thực không hợp lệ"
→ server kiểm captcha thật; model g_300x75 được nhận 1/1. Không đòi token reCAPTCHA.
Mã tra cứu: TTKhac "MaTraCuu" như CyberLotus (chưa đối chiếu trên XML thật của Liên Sơn).
"""
from __future__ import annotations

from . import register
from .xcyber import XcyberAdapter


class LienSonAdapter(XcyberAdapter):
    source_id = "lienson"
    name = "Liên Sơn (xcyber)"
    mst_tcgp = ("0301452923",)
    hosts = ("lienson.vn",)
    SYSTEMS = (("https://ls-invoiceapp.lienson.vn", "https://tracuu.lienson.vn"),)


register(LienSonAdapter())
