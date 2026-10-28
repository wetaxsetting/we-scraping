"""
WinInvoice (Công ty TNHH Win Tech Solution) — MSTTCGP 0312303803.
Tra cứu: https://tracuu.wininvoice.vn  (tracuu.evat.vn cũ nay chuyển hướng về đây).

Cùng phần mềm PHP với nền tảng eVAT (pdf_providers/evat.py): form action="/bill" giống hệt
(private_code + cmpn_key + CSRF ẩn tên "phtcsrf_tkndf-tc"), kết quả /bill/inv có link
/bill/view?...&download=1 → ZIP chứa PDF + XML có chữ ký. Nên adapter này chỉ đổi cổng,
còn lại dùng lại toàn bộ luồng của EvatAdapter.

Mã tra cứu (TTKhac cấp hóa đơn trong XML WinInvoice, kiểm 26/10/2026 trên XML thật):
    "Mã tra cứu hóa đơn"  vd 535578E950 (10 ký tự hex/chữ hoa) — cũng in trên PDF / mail
    "Mã công ty"          = MST người bán (0317343246) — form BẮT BUỘC có, để trống trả
                            "Vui lòng nhập đầy đủ thông tin", MST sai trả "Mã công ty không tồn tại"
Không captcha: trang có nạp recaptcha/api.js nhưng form không có widget, server không đòi token.

Kiểm thật 26/10/2026 (HĐ mua vào của công ty, mã lấy từ XML đính kèm mail): 5/5 tải được PDF
có đúng số HĐ và MST người mua 1201496252 — Thiên Lý C25MTL-175268, GENUSS C24TAA-104,
Việt Uy Tín C23TUT-2325, The Only Way C25MMP-6024, Hộp Gia Vị C25MTM-815.

Link trong mail (tracuu.wininvoice.vn, tracuu.evat.vn, wininvoice.vn/bghddt.php, ảnh logo):
mail chỉ có mã tra cứu, KHÔNG có "Mã công ty" → không tra được bằng form; 28/29 mail WinInvoice
đã kèm sẵn PDF + XML nên không cần link. Chỉ nhận link khi nội dung mail có ghi mã công ty.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import register
from .evat import _CMPN_RE, _CODE_RE, EvatAdapter

HOST = "tracuu.wininvoice.vn"


class WinInvoiceAdapter(EvatAdapter):
    source_id = "wininvoice"
    name = "WinInvoice (tracuu.wininvoice.vn)"
    # 0311174986 Trường Minh Thịnh: Excel NCC ghi cổng tra cứu là tracuu.wininvoice.vn (đại lý dùng chung
    # cổng/CSDL WinInvoice; form tra theo "Mã công ty" = MST người bán nên không phụ thuộc MSTTCGP).
    # Thêm 27/10/2026, chưa có HĐ thật — chỉ kiểm form /bill còn nguyên trên cổng.
    mst_tcgp = ("0312303803", "0311174986")
    hosts = ("wininvoice.vn", "tracuu.evat.vn")     # gồm tracuu.wininvoice.vn

    @staticmethod
    def portal(invoice: dict) -> str:
        return HOST

    def invoice_from_link(self, url: str, code, mail_text: str):
        """Chỉ dùng được khi mail ghi "Mã công ty: <MST bán>" (form bắt buộc). Không mở link."""
        host = (urlsplit(url or "").hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return None
        code = (code or "").strip()
        m = _CMPN_RE.search(mail_text or "")
        if not _CODE_RE.match(code) or not m:
            return None
        return {"ma_tra_cuu": code, "nbmst": m.group(1), "mst_tcgp": self.mst_tcgp[0],
                "tt_khac": {"Mã tra cứu hóa đơn": code, "Mã công ty": m.group(1)}}


register(WinInvoiceAdapter())
