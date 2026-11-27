"""
Chi tiết HĐ từ API "Xem hóa đơn" của cổng GDT (`api_client.fetch_invoice_detail_json`) → dòng hàng
`invoice_details` (nguon='gdt_detail') + phần đầu HĐ trộn vào `invoices.list_raw`.

Dùng khi Thuế không có XML (export-xml trả 500):
  - HĐ gửi theo bảng tổng hợp (hthdon=2): GDT chỉ có những gì người bán khai trong bảng tổng hợp →
    mỗi thuế suất 1 dòng (thành tiền, tiền thuế; đôi khi tên dịch vụ, đơn vị, số lượng — vd điện kWh).
  - HĐ thường mà XML chưa tải được (hthdon≠2): đủ dòng hàng như XML.
XML thật (Thuế / mail / tải lên) về sau sẽ ghi đè các dòng này (`replace_details`).
Đo 27/11/2026: 10/10 HĐ trả 200; trong 3.066 HĐ `summary_only` có 2.878 HĐ lấy từ file Excel (không
có hthdon) — ít nhất 1 HĐ trong số đó thật ra hthdon=0 (WinMart K26TTM-2254, GDT có đủ dòng hàng).
"""
from __future__ import annotations

from datetime import datetime, timezone

NGUON = "gdt_detail"

# Trường phần đầu HĐ giữ lại (trộn vào invoices.list_raw — cùng tên khóa với API danh sách JSON)
HEADER_KEYS = (
    "hthdon", "msttcgp", "mhdon", "mkhang", "nbdchi", "nbsdthoai", "nbstkhoan", "nbtnhang", "nbwebsite",
    "nmdchi", "nmsdthoai", "nmstkhoan", "nmtnhang", "nmtnmua", "dvtte", "tgia", "thtttoan", "htttoan",
    "tgtcthue", "tgtthue", "tgtttbso", "tgtttbchu", "tgtkhac", "tgtphi", "ttcktmai", "thttltsuat", "ttkhac",
    "khhdgoc", "khmshdgoc", "shdgoc", "tdlhdgoc", "lhdgoc", "gchdgoc", "tlhdon", "thdon",
)


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rate(line: dict) -> str | None:
    """Thuế suất dạng chữ như XML (8%, 10%, KCT, KKKNT…): `ltsuat`, thiếu thì suy từ `tsuat` (0.08)."""
    label = str(line.get("ltsuat") or "").strip()
    if label:
        return label
    ts = _num(line.get("tsuat"))
    if ts is None:
        return None
    pct = ts * 100
    return f"{pct:g}%"


def summary_table(js: dict) -> bool:
    """HĐ gửi Thuế theo bảng tổng hợp → dòng hàng chỉ là tổng theo thuế suất."""
    return str(js.get("hthdon")) == "2"


def items(js: dict) -> list[dict]:
    """`hdhhdvu` → dòng hàng cùng khóa với `xml_parser` (`replace_details`)."""
    lines = sorted(js.get("hdhhdvu") or [],
                   key=lambda h: (_num(h.get("sxep")) or 0, _num(h.get("stt")) or 0))
    summary = summary_table(js)
    out = []
    for i, h in enumerate(lines, 1):
        thanh_tien, tien_thue = _num(h.get("thtien")), _num(h.get("tthue"))
        out.append({
            # bảng tổng hợp: `stt` là số dòng trong cả bảng (vd 4965), không phải thứ tự trong HĐ
            "stt": i if summary else (int(_num(h.get("stt"))) if _num(h.get("stt")) is not None else i),
            "tinh_chat": int(_num(h.get("tchat"))) if _num(h.get("tchat")) is not None else None,
            "ma_hhdv": h.get("mhhdvu") or None,
            "ten_hhdv": (h.get("ten") or "").strip() or None,
            "don_vi_tinh": (h.get("dvtinh") or "").strip() or None,
            "so_luong": _num(h.get("sluong")),
            "don_gia": _num(h.get("dgia")),
            "tl_ck": _num(h.get("tlckhau")),
            "st_ck": _num(h.get("stckhau")),
            "thanh_tien": thanh_tien,
            "thue_suat": _rate(h),
            "tien_thue": tien_thue,
            "tong_tien": thanh_tien + tien_thue if thanh_tien is not None and tien_thue is not None else None,
            "ma_nhom_nganh": h.get("mnnnghe") or None,
        })
    return out


def header(js: dict) -> dict:
    """Phần đầu HĐ (khóa khác rỗng) + dấu `_gdt_detail_at` (đã xem chi tiết, không gọi lại)."""
    out = {k: js[k] for k in HEADER_KEYS if js.get(k) not in (None, "", [], {})}
    out["_gdt_detail_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def hthdon(js: dict) -> int | None:
    v = _num(js.get("hthdon"))
    return int(v) if v is not None else None
