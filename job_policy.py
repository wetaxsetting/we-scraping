# job_policy.py
"""
Các quyết định của job lấy hóa đơn, tách riêng để test được mà không cần DB/mạng:

    next_retry()        lỗi loại X, đã thử N lần → chờ đến khi nào, hay bỏ (dead)
    sync_windows()      cần đồng bộ danh sách những khoảng ngày nào (mỗi khoảng ≤ 30 ngày)
    CircuitBreaker      GDT lỗi liên tiếp → dừng lượt chạy, không gọi dồn
    normalize_list_row  dòng API danh sách → giá trị cột bảng invoices
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from api_client import SUB_TYPE_LABEL, tdlap_vn

# ---------------------------------------------------------------------------
# Thử lại
# ---------------------------------------------------------------------------

# kind → (thời gian chờ trước lần thử sau, số lần thử tối đa).
# Job chạy 2–3 lần/ngày, nên chờ 30 phút nghĩa là "lượt chạy sau".
RETRY_POLICY = {
    "network":     (timedelta(minutes=30), 15),
    "server":      (timedelta(minutes=30), 15),
    "rate_limit":  (timedelta(hours=1), 15),
    "auth":        (timedelta(minutes=30), 15),  # GDT vẫn từ chối sau khi đăng nhập lại
    "not_ready":   (timedelta(days=1), 10),     # GDT 500 "XML chưa có" — thường có sau vài ngày
    "captcha":     (timedelta(minutes=30), 15),  # PDF: model đọc sai captcha site tra cứu
    "no_pdf":      (timedelta(days=1), 7),       # PDF: site tra cứu chưa có HĐ
    "bad_content": (timedelta(hours=6), 3),      # trả về không phải ZIP/PDF, đôi khi tạm thời
    "parse":       (None, 1),                    # XML hỏng → xem tay
    "no_code":     (None, 1),                    # PDF: XML không có mã tra cứu → không thể tra
    "other":       (timedelta(hours=6), 5),
}

# Lỗi do phía GDT/site (không phải do riêng hóa đơn) → tính vào ngắt sớm.
TRANSIENT_KINDS = frozenset({"network", "server", "rate_limit"})


def next_retry(kind: str, attempts: int, now: datetime | None = None):
    """
    Sau lần thất bại thứ `attempts` với lỗi `kind`:
        → ("pending", thời điểm thử lại)  hoặc  ("dead", None)
    """
    now = now or datetime.now(timezone.utc)
    delay, max_attempts = RETRY_POLICY.get(kind, RETRY_POLICY["other"])
    if delay is None or attempts >= max_attempts:
        return "dead", None
    return "pending", now + delay


class CircuitBreaker:
    """
    Đếm lỗi tạm thời LIÊN TIẾP. Đủ ngưỡng → open: dừng gọi tiếp trong lượt này,
    phần còn lại giữ nguyên chờ lượt sau. Lỗi riêng của 1 hóa đơn (not_ready,
    parse...) không tính — đó không phải dấu hiệu GDT đang quá tải.
    """

    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self.consecutive = 0

    def record(self, ok: bool, kind: str | None = None) -> None:
        if ok:
            self.consecutive = 0
        elif kind in TRANSIENT_KINDS:
            self.consecutive += 1

    @property
    def open(self) -> bool:
        return self.consecutive >= self.threshold


# ---------------------------------------------------------------------------
# Khoảng ngày đồng bộ
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 30   # luôn quét lại 30 ngày gần nhất (HĐ lên GDT muộn)
MAX_SPAN_DAYS = 30   # GDT giới hạn mỗi lần tìm ≤ 30 ngày


def chunk_range(start: date, end: date, span_days: int = MAX_SPAN_DAYS) -> list:
    """[start, end] → các đoạn liên tiếp, mỗi đoạn ≤ span_days ngày (tính cả 2 đầu)."""
    out = []
    s = start
    while s <= end:
        e = min(s + timedelta(days=span_days - 1), end)
        out.append((s, e))
        s = e + timedelta(days=1)
    return out


def sync_windows(cursor: date | None, today: date,
                 lookback_days: int = LOOKBACK_DAYS,
                 span_days: int = MAX_SPAN_DAYS) -> list:
    """
    Khoảng cần đồng bộ cho 1 sub-type:
      - bình thường: 30 ngày gần nhất (rolling, KHÔNG theo tháng lịch)
      - job nghỉ lâu (cursor cũ hơn 30 ngày): từ cursor đến hôm nay, chia đoạn ≤ 30 ngày
    cursor = ngày cuối cùng đã đồng bộ thành công.
    """
    start = today - timedelta(days=lookback_days - 1)
    if cursor is not None and cursor < start:
        start = cursor
    return chunk_range(start, today, span_days)


def month_bounds(month: str | None, today: date) -> tuple:
    """'2026-09' → (01/09, 30/09). None → tháng trước của `today`."""
    if month:
        y, m = (int(x) for x in month.split("-"))
        first = date(y, m, 1)
    else:
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


# Ngày trong tháng quét lại toàn bộ tháng trước (trước hạn kê khai ngày 20).
SWEEP_DAYS = (5, 15)


# ---------------------------------------------------------------------------
# Chuẩn hóa dữ liệu
# ---------------------------------------------------------------------------

_MST_RE = re.compile(r"^\d{10}(-\d{3})?$")


def is_valid_mst(mst: str | None) -> bool:
    return bool(mst) and bool(_MST_RE.match(mst.strip()))


def _decimal(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_list_row(row: dict, company_mst: str, sub_type: str) -> dict | None:
    """
    Dòng API danh sách → giá trị cho bảng invoices. Thiếu khóa định danh → None.
    Ngày lập đổi sang giờ VN (tdlap của GDT là UTC).
    """
    nbmst = (row.get("nbmst") or "").strip()
    khhdon = (row.get("khhdon") or "").strip()
    shdon = _int_or_none(row.get("shdon"))
    if not nbmst or not khhdon or shdon is None:
        return None
    return {
        "company_mst": company_mst,
        "direction": sub_type.split("-")[0],
        "sub_type": sub_type,
        "loai_hd": SUB_TYPE_LABEL.get(sub_type, sub_type),
        "nbmst": nbmst,
        "nbten": row.get("nbten"),
        "nmmst": (row.get("nmmst") or "").strip() or None,
        "nmten": row.get("nmten"),
        "khmshdon": _int_or_none(row.get("khmshdon")) or 1,
        "khhdon": khhdon,
        "shdon": shdon,
        "ngay_lap": tdlap_vn(row),
        "tien_chua_thue": _decimal(row.get("tgtcthue")),
        "tien_thue": _decimal(row.get("tgtthue")),
        "tong_tien": _decimal(row.get("tgtttbso")),
        "tthai": _int_or_none(row.get("tthai")),
        "hthdon": _int_or_none(row.get("hthdon")),
        "list_raw": row,
    }


# Hình thức HĐ mà Thuế chỉ nhận dữ liệu tổng hợp, không có XML từng HĐ. Dữ liệu thật
# 10/2026: hthdon=2 → 19/19 HĐ export-xml trả 500; hthdon 0/1/5 → 54/54 có XML.
HTHDON_CHI_TONG_HOP = 2


def summary_only(row: dict) -> bool:
    """HĐ gửi Thuế theo bảng tổng hợp → không có XML trên Thuế, không nên thử tải."""
    return row.get("hthdon") == HTHDON_CHI_TONG_HOP
