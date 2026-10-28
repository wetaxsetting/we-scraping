"""
Gom mã tra cứu ứng viên từ dict HĐ (TTKhac + ma_tra_cuu) cho các adapter chưa có XML thật để biết
đúng tên trường TTKhac (lô adapter 27/10/2026: mmhoadon, gohoadon, epay, atom, suninvoice, winvoice,
vnpost). Không phải adapter — không đăng ký gì.

Thứ tự: các khóa adapter khai báo (đúng tên) → mọi trường TTKhac có tên giống "mã tra cứu" /
"số bảo mật" (so sánh không dấu, không khoảng trắng) → invoice["ma_tra_cuu"] (parser đoán).
"""
from __future__ import annotations

import re
import unicodedata

_HINTS = ("tracuu", "mtcuu", "matc", "baomat", "lookup", "searchkey", "privatecode", "secret")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s)).replace("đ", "d").replace("Đ", "D")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def lookup_codes(invoice: dict, keys: tuple = (), pattern: re.Pattern | None = None) -> list:
    """Danh sách mã (bỏ trùng/rỗng; `pattern` lọc dạng mã, vd bỏ URL)."""
    tt_khac = invoice.get("tt_khac") or {}
    cands = [tt_khac.get(k) for k in keys]
    cands += [v for k, v in tt_khac.items() if any(h in _norm(k) for h in _HINTS)]
    cands.append(invoice.get("ma_tra_cuu"))
    codes = (str(c).strip() for c in cands if c and str(c).strip())
    return [c for c in dict.fromkeys(codes) if pattern is None or pattern.match(c)]
