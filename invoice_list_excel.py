# invoice_list_excel.py
"""
Đọc file Excel "DANH SÁCH HÓA ĐƠN" do GDT xuất (nút "Xuất Excel" trên hoadondientu.gdt.gov.vn,
API .../export-excel và .../export-excel-sold) → list dict, khóa đặt theo tên trường của API danh
sách JSON (nbmst, khhdon, shdon, tdlap, tgtttbso...) để job_policy.normalize_list_row dùng chung.

Bố cục file (10/2026): dòng 3 tiêu đề, dòng 4 "Từ ngày ... đến ngày ...", dòng 6 tên cột, dữ liệu
từ dòng 7. HĐ thường có 19 cột; HĐ máy tính tiền 17 cột theo thứ tự khác (thêm "Địa chỉ người bán",
"Căn cước công dân"; không có tiền tệ, tỷ giá, tiền phí) → dò cột theo tên, không theo vị trí.

File Excel KHÔNG có: hthdon (HĐ gửi Thuế theo bảng tổng hợp), giờ lập, MSTTCGP, id của GDT.
"""
from __future__ import annotations

import io
import re
import unicodedata
import warnings
import zipfile
from datetime import date, datetime, timedelta, timezone

import openpyxl

_VN_TZ = timezone(timedelta(hours=7))

# Tên cột trong file → khóa của API danh sách JSON
_COLUMNS = {
    "ký hiệu mẫu số": "khmshdon",
    "ký hiệu hóa đơn": "khhdon",
    "số hóa đơn": "shdon",
    "ngày lập": "tdlap",
    "mst người bán/mst người xuất hàng": "nbmst",
    "tên người bán/tên người xuất hàng": "nbten",
    "địa chỉ người bán": "nbdchi",
    "mst người mua/mst người nhận hàng": "nmmst",
    "tên người mua/tên người nhận hàng": "nmten",
    "địa chỉ người mua": "nmdchi",
    "căn cước công dân": "nmcccd",
    "tổng tiền chưa thuế": "tgtcthue",
    "tổng tiền thuế": "tgtthue",
    "tổng tiền chiết khấu thương mại": "ttcktmai",
    "tổng tiền phí": "tgtphi",
    "tổng tiền thanh toán": "tgtttbso",
    "đơn vị tiền tệ": "dvtte",
    "tỷ giá": "tgia",
    "trạng thái hóa đơn": "tthai",
    "kết quả kiểm tra hóa đơn": "ttxly",
}
_REQUIRED = ("khhdon", "shdon", "nbmst", "tdlap")

# Trạng thái HĐ (chữ) → mã tthai của API. 1–2 đã đối chiếu với HĐ thật trong DB;
# 3–6 chưa gặp trong file nào.
_TTHAI = {
    "hóa đơn mới": 1,
    "hóa đơn thay thế": 2,
    "hóa đơn điều chỉnh": 3,
    "hóa đơn đã bị thay thế": 4,
    "hóa đơn đã bị điều chỉnh": 5,
    "hóa đơn đã bị hủy": 6,
    "hóa đơn đã bị huỷ": 6,
}


def _key(text) -> str:
    """So khớp chữ không phụ thuộc hoa/thường, khoảng trắng thừa, dạng Unicode."""
    s = unicodedata.normalize("NFC", str(text or ""))
    return re.sub(r"\s+", " ", s).strip().lower()


def _ttxly(text):
    """Kết quả kiểm tra (chữ) → mã ttxly của API: 5 có mã, 6 không mã, 8 máy tính tiền."""
    k = _key(text)
    if "máy tính tiền" in k:
        return 8
    if "không mã" in k:
        return 6
    if "cấp mã" in k:
        return 5
    return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _num(v):
    """Ô tiền: số giữ nguyên; chữ thì thử đọc thành số."""
    if v is None or isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def _mst(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return f"{int(v):010d}"            # ô kiểu số làm rơi số 0 đầu của MST
    return str(v).strip() or None


def _tdlap(v) -> str:
    """'22/09/2026' (ngày VN) → '2026-09-21T17:00:00Z': cùng dạng UTC như tdlap của API JSON."""
    if isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    else:
        d = datetime.strptime(str(v or "").strip(), "%d/%m/%Y").date()
    utc = datetime(d.year, d.month, d.day, tzinfo=_VN_TZ).astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(data: bytes) -> list:
    """Byte file .xlsx → list dict, mỗi dict 1 HĐ. File không đúng mẫu → ValueError."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")     # file GDT (Apache POI) không có style mặc định
            # KHÔNG dùng read_only: file GDT ghi <dimension ref="A1"/>, chế độ đó chỉ thấy 1 dòng.
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as e:
        raise ValueError(f"không mở được file Excel: {e}") from e

    rows = wb.worksheets[0].iter_rows(values_only=True)
    header = None
    for i, values in enumerate(rows):
        names = [_key(v) for v in values]
        if "ký hiệu hóa đơn" in names and "số hóa đơn" in names:
            header = [str(v).strip() if v is not None else "" for v in values]
            break
        if i >= 30:
            break
    if header is None:
        raise ValueError("không thấy dòng tên cột (Ký hiệu hóa đơn, Số hóa đơn)")
    fields = [_COLUMNS.get(_key(h)) for h in header]
    missing = [f for f in _REQUIRED if f not in fields]
    if missing:
        names = {v: k for k, v in _COLUMNS.items()}
        raise ValueError(f"thiếu cột: {', '.join(names[f] for f in missing)}")

    out = []
    for values in rows:                         # tiếp ngay sau dòng tên cột
        if all(v in (None, "") for v in values):
            continue
        rec = {}
        for name, field, v in zip(header, fields, values):
            if field:
                rec[field] = v
            elif name and _key(name) != "stt" and v not in (None, ""):
                rec[name] = v                   # cột GDT mới thêm: giữ nguyên tên để xem lại
        rec["khmshdon"] = _int(rec.get("khmshdon"))
        rec["khhdon"] = str(rec.get("khhdon") or "").strip()
        rec["shdon"] = _int(rec.get("shdon"))
        try:
            rec["tdlap"] = _tdlap(rec.get("tdlap"))
        except ValueError as e:
            raise ValueError(f"ngày lập không đúng dạng dd/MM/yyyy: {rec.get('tdlap')!r}") from e
        for k in ("nbmst", "nmmst"):
            if k in rec:
                rec[k] = _mst(rec[k])
        for k in ("tgtcthue", "tgtthue", "ttcktmai", "tgtphi", "tgtttbso", "tgia"):
            if k in rec:
                rec[k] = _num(rec[k])
        if "tthai" in rec:
            rec["_tthai_text"] = rec["tthai"]
            rec["tthai"] = _TTHAI.get(_key(rec["tthai"]))
        if "ttxly" in rec:
            rec["_ttxly_text"] = rec["ttxly"]
            rec["ttxly"] = _ttxly(rec["ttxly"])
        rec["_nguon"] = "excel"
        out.append(rec)
    return out
