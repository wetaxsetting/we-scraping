"""
Parse XML hóa đơn điện tử theo QĐ 1233/QĐ-CT ngày 27/08/2026 của Cục Thuế
(chuẩn dữ liệu PBan 2.1.1). Đọc được cả XML chuẩn cũ PBan 2.1.0 — cấu trúc thẻ
giống nhau, chuẩn mới chỉ bổ sung thẻ.

Cấu trúc (Phần II, Mục II QĐ 1233):

    HDon
    ├─ DLHDon                         dữ liệu do người bán lập
    │  ├─ TTChung                     thông tin chung (+ TTHDLQuan nếu là HĐ điều chỉnh/thay thế)
    │  ├─ NDHDon
    │  │  ├─ NBan, NMua               người bán, người mua
    │  │  ├─ DSHHDVu/HHDVu*           dòng hàng (+ TTHHDTrung: hàng hóa đặc trưng)
    │  │  └─ TToan                    THTTLTSuat/LTSuat* (tổng theo thuế suất), DSLPhi/LPhi*, tổng tiền
    │  └─ TTKhac                      thông tin khác do NNT tự định nghĩa (TTKhac có thể ở nhiều cấp)
    ├─ DSHDGiay/DLHDGiay*             hóa đơn giấy liên quan (nếu có)
    ├─ DLQRCode                       chuỗi QR, định dạng TLV (Phần I, Mục IV, Khoản 7)
    ├─ MCCQT                          mã CQT: 34 ký tự; HĐ máy tính tiền: 23 ký tự M#-YY-XXXXX-###########
    └─ DSCKS                          chữ ký số: NBan, NMua, CQT, CCKSKhac

Ngày trong XML là giờ VN (GMT+7), giữ nguyên dạng YYYY-MM-DD.
Số trả về dạng số (int nếu nguyên, float nếu có phần thập phân), không định dạng hiển thị.

Dùng:
    from xml_parser import parse_zip, parse_xml_bytes
    detail = parse_zip(zip_bytes)
    detail["canh_bao"]   # các điểm lệch so với quy định (thiếu thẻ bắt buộc, tổng tiền lệch...)
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Danh mục (phụ lục QĐ 1233)
# ---------------------------------------------------------------------------

# Phụ lục II — ký hiệu mẫu số hóa đơn (NĐ 254/2026, TT 91/2026)
KY_HIEU_MAU_SO = {
    "1": "Hóa đơn giá trị gia tăng",
    "2": "Hóa đơn bán hàng",
    "3": "Hóa đơn bán tài sản công",
    "4": "Hóa đơn bán hàng dự trữ quốc gia",
    "5": "Tem, vé, thẻ, phiếu thu điện tử và chứng từ khác có nội dung hóa đơn",
    "6": "Phiếu xuất kho kiêm vận chuyển nội bộ / hàng gửi bán đại lý",
    "7": "Hóa đơn thương mại",
    "8": "Hóa đơn GTGT tích hợp biên lai thu thuế, phí, lệ phí",
    "9": "Hóa đơn bán hàng tích hợp biên lai thu thuế, phí, lệ phí",
}

# Phụ lục IV — tính chất dòng hàng
TINH_CHAT = {
    1: "Hàng hóa, dịch vụ",
    2: "Khuyến mại",
    3: "Chiết khấu thương mại",
    4: "Ghi chú/diễn giải",
    5: "Hàng hóa đặc trưng",
}

# Phụ lục V — thuế suất không phải dạng phần trăm
THUE_SUAT_KHAC = {
    "KCT": "Không chịu thuế GTGT",
    "KKKNT": "Không kê khai, tính nộp thuế GTGT",
    "CTTC": "Thuế suất của hóa đơn cho thuê tài chính",
    "KTPVDC": "Không thuộc phạm vi điều chỉnh của Luật thuế",
    "KTGTT": "Không tính vào giá tính thuế GTGT",
}

# TTHDLQuan/TCHDon — tính chất hóa đơn
TINH_CHAT_HOA_DON = {
    1: "Thay thế",
    2: "Điều chỉnh",
    3: "Thay thế nhiều hóa đơn",
    4: "Điều chỉnh nhiều hóa đơn",
    5: "Chiết khấu thương mại",
}

# Phụ lục VI — loại hóa đơn có liên quan (bị thay thế/điều chỉnh)
LOAI_HD_LIEN_QUAN = {
    1: "HĐĐT theo NĐ 123/2020, NĐ 70/2025",
    2: "HĐĐT có mã xác thực theo QĐ 1209/QĐ-BTC, 2660/QĐ-BTC",
    3: "Hóa đơn theo NĐ 51/2010, NĐ 04/2014",
    4: "Hóa đơn đặt in theo NĐ 123/2020",
    5: "HĐĐT theo NĐ 254/2026",
}

# Phụ lục XVII — lý do điều chỉnh/thay thế
LY_DO_DIEU_CHINH = {
    1: "Điều chỉnh giá trị quyết toán dự án / giá bán theo pháp luật chuyên ngành",
    2: "Điều chỉnh theo kết luận của cơ quan nhà nước có thẩm quyền",
    3: "Điều chỉnh giá bán buôn điện (EVN)",
    4: "Điều chỉnh do chiết khấu thương mại",
    5: "Điều chỉnh do trả lại hàng hóa (không phải tài sản phải đăng ký)",
    6: "Thay thế do trả lại hàng hóa là tài sản phải đăng ký",
    7: "Điều chỉnh do hoàn phí, giảm phí bảo hiểm",
    8: "Điều chỉnh do hủy/chấm dứt giao dịch, hủy một phần dịch vụ thu trước",
    9: "Điều chỉnh do hoàn phí của tổ chức tín dụng, trung gian thanh toán",
    10: "Điều chỉnh đối với dịch vụ viễn thông thanh toán bằng thẻ trả trước",
    11: "Điều chỉnh do thay đổi giá trị bán khí thiên nhiên khi quy đổi VND",
    12: "Điều chỉnh đối với hóa đơn đổi, hoàn chứng từ vận chuyển hàng không",
    13: "Điều chỉnh/thay thế cho hóa đơn đã lập sai",
    14: "Thay thế hóa đơn máy tính tiền đã lập sai",
}

PHIEN_BAN_DA_BIET = {"2.0.0", "2.0.1", "2.1.0", "2.1.1"}

_MCCQT_MTT_RE = re.compile(r"^M\d-\d{2}-[A-Z0-9]{5}-\d{11}$")
_MST_RE = re.compile(r"^\d{10}(-\d{3})?$")

# ---------------------------------------------------------------------------
# Thẻ → khóa kết quả. "core" luôn có trong kết quả (rỗng nếu thiếu thẻ);
# "extra" chỉ có khi XML có thẻ đó (theo loại hóa đơn).
# ---------------------------------------------------------------------------

_NBAN_CORE = {
    "Ten": "ten", "MST": "mst", "DChi": "dia_chi", "SDThoai": "dien_thoai",
    "DCTDTu": "email", "STKNHang": "so_tk", "TNHang": "ngan_hang",
}
_NBAN_EXTRA = {
    "Fax": "fax", "Website": "website",
    "MCHang": "ma_cua_hang", "TCHang": "ten_cua_hang",
    "MDDKDoanh": "ma_dia_diem_kd", "TDDKDoanh": "ten_dia_diem_kd", "DCDDKDoanh": "dia_chi_dia_diem_kd",
    "TCQDGBTSan": "ten_co_quan_ban_dau_gia", "DCCQDGBTSan": "dia_chi_co_quan_ban_dau_gia",
    "MSTCQDGBTSan": "mst_co_quan_ban_dau_gia",
    # Hóa đơn bán tài sản công
    "MDVQHNSach": "ma_dvqhns", "SQDinh": "so_quyet_dinh", "NQDinh": "ngay_quyet_dinh",
    "CQBHQDinh": "co_quan_ban_hanh_qd", "HTBan": "hinh_thuc_ban",
    # Phiếu xuất kho
    "LDDNBo": "lenh_dieu_dong", "HDSo": "hop_dong_van_chuyen_so",
    "HDKTSo": "hop_dong_kinh_te_so", "HDKTNgay": "hop_dong_kinh_te_ngay",
    "HVTNXHang": "nguoi_xuat_hang", "TNVChuyen": "nguoi_van_chuyen",
    "PTVChuyen": "phuong_tien_van_chuyen",
}
_NMUA_CORE = {
    "Ten": "ten", "MST": "mst", "DChi": "dia_chi", "DCTDTu": "email", "MKHang": "ma_khach",
    "HVTNMHang": "nguoi_mua_hang", "SDThoai": "dien_thoai",
    "STKNHang": "so_tk", "TNHang": "ngan_hang",
}
_NMUA_EXTRA = {
    "MDVQHNSach": "ma_dvqhns", "CCCDan": "cccd", "SHChieu": "ho_chieu",
    "SDDTCNNgoai": "so_dinh_danh_to_chuc_nn", "MQTNMua": "ma_quoc_tich", "QTNMua": "quoc_tich",
    "MTinh": "ma_tinh", "TTinh": "ten_tinh", "MXa": "ma_xa", "TXa": "ten_xa",
    "HVTNNHang": "nguoi_nhan_hang",                                   # phiếu xuất kho
    "DDVCHDen": "dia_diem_van_chuyen_den",                            # bán tài sản công
    "TGVCHDTu": "thoi_gian_van_chuyen_tu", "TGVCHDDen": "thoi_gian_van_chuyen_den",
}

# ---------------------------------------------------------------------------
# Helpers đọc cây XML (theo đường dẫn thẻ con trực tiếp, đúng cấu trúc QĐ 1233)
# ---------------------------------------------------------------------------


def _ln(tag: str) -> str:
    """Bỏ namespace: '{ns}Ten' → 'Ten'."""
    return tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    if el is None:
        return None
    return next((c for c in el if _ln(c.tag) == name), None)


def _children(el, name: str) -> list:
    return [c for c in el if _ln(c.tag) == name] if el is not None else []


def _path(el, *names):
    for n in names:
        el = _child(el, n)
    return el


def _t(el, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


class _Ctx:
    """Gom cảnh báo trong lúc parse."""

    def __init__(self):
        self.warnings: list = []

    def num(self, el, name: str):
        raw = _t(el, name)
        if not raw:
            return None
        try:
            d = Decimal(raw)
        except InvalidOperation:
            self.warnings.append(f"Thẻ {name} không phải số: {raw!r}")
            return None
        return int(d) if d == d.to_integral_value() else float(d)

    def int_(self, el, name: str):
        v = self.num(el, name)
        return int(v) if isinstance(v, (int, float)) else None


def _fields(el, core: dict, extra: dict) -> dict:
    out = {key: _t(el, tag) for tag, key in core.items()}
    for tag, key in extra.items():
        if _child(el, tag) is not None:
            out[key] = _t(el, tag)
    return out


def _ttkhac(el) -> dict:
    """TTKhac (con trực tiếp của el) → {TTruong: DLieu}."""
    out = {}
    for tk in _children(el, "TTKhac"):
        for tin in _children(tk, "TTin"):
            name = _t(tin, "TTruong")
            if name and name not in out:
                out[name] = _t(tin, "DLieu")
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def extract_xml(zip_bytes: bytes) -> bytes:
    """Lấy bytes của file .xml (đầu tiên) trong ZIP của GDT. Không có → ValueError."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise ValueError(f"ZIP hỏng: {e}") from e
    xml_name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
    if xml_name is None:
        raise ValueError("ZIP không chứa file XML")
    return z.read(xml_name)


def parse_zip(zip_bytes: bytes) -> dict:
    """Giải nén ZIP của GDT, lấy XML đầu tiên, parse → dict."""
    return parse_xml_bytes(extract_xml(zip_bytes))


def parse_xml_bytes(raw: bytes) -> dict:
    """Parse XML bytes → dict (tự dò encoding, chuẩn là UTF-8)."""
    xml_str = None
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            xml_str = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if xml_str is None:
        raise ValueError("Không đọc được XML (encoding)")
    root = ET.fromstring(xml_str)
    hdon = root if _ln(root.tag) == "HDon" else next(
        (e for e in root.iter() if _ln(e.tag) == "HDon"), None)
    if hdon is None:
        raise ValueError(f"Không có thẻ HDon (thẻ gốc: {_ln(root.tag)})")
    result = _parse_hdon(hdon)
    result["lookup_url"] = _find_lookup_url(xml_str)
    return result


# ---------------------------------------------------------------------------
# Các phần của hóa đơn
# ---------------------------------------------------------------------------

def _parse_hdon(hdon) -> dict:
    ctx = _Ctx()
    dl = _child(hdon, "DLHDon")
    tc = _child(dl, "TTChung")
    nd = _child(dl, "NDHDon")
    ttoan = _child(nd, "TToan")

    tt_khac = _invoice_level_ttkhac(dl)
    result = {
        "thong_tin_chung": _parse_ttchung(tc, ctx),
        "nguoi_ban": _fields(_child(nd, "NBan"), _NBAN_CORE, _NBAN_EXTRA),
        "nguoi_mua": _fields(_child(nd, "NMua"), _NMUA_CORE, _NMUA_EXTRA),
        "hang_hoa": [_parse_hhdvu(h, ctx) for h in _children(_child(nd, "DSHHDVu"), "HHDVu")],
        "thanh_toan": _parse_ttoan(ttoan, ctx) if ttoan is not None else {},
        "ma_cqt": _t(hdon, "MCCQT"),
        "qr": parse_qr(_t(hdon, "DLQRCode")),
        "ky_so": _parse_dscks(_child(hdon, "DSCKS")),
        "tt_khac": tt_khac,
        # VNPT để Fkey ngay dưới <HDon> (ngoài DLHDon) — dùng khi TTKhac không có mã tra cứu
        "ma_tra_cuu": _guess_ma_tra_cuu(tt_khac) or _t(hdon, "Fkey"),
        # Id của DLHDon do NCC đặt; BKAV dùng làm InvoiceGUID để mở trang tra cứu
        "dlhdon_id": (dl.get("Id") or "").strip() if dl is not None else "",
    }
    giay = [_parse_hd_giay(g) for g in _children(_child(hdon, "DSHDGiay"), "DLHDGiay")]
    if giay:
        result["hoa_don_giay"] = giay
    result["canh_bao"] = ctx.warnings + _validate(result, dl, ttoan)
    return result


def _parse_ttchung(tc, ctx: _Ctx) -> dict:
    khms = _t(tc, "KHMSHDon")
    khhd = _t(tc, "KHHDon")
    so_hd = ctx.int_(tc, "SHDon")
    out = {
        "phien_ban": _t(tc, "PBan"),
        "loai_hd": _t(tc, "THDon"),                     # tên hóa đơn ghi trên XML
        "ky_hieu_mau": khms,
        "ten_loai_mau": KY_HIEU_MAU_SO.get(khms, ""),
        "ky_hieu": khhd,
        "so_hd": so_hd if so_hd is not None else _t(tc, "SHDon"),
        "ngay_lap": _t(tc, "NLap"),
        "don_vi_tt": _t(tc, "DVTTe") or "VND",
        "ty_gia": ctx.num(tc, "TGia"),
        "hinh_thuc_tt": _t(tc, "HTTToan"),
        "mst_tcgp": _t(tc, "MSTTCGP"),
        # Ký tự đầu ký hiệu: C = có mã của CQT, K = không mã (Phụ lục I TT 91)
        "co_ma": {"C": True, "K": False}.get(khhd[:1].upper()) if khhd else None,
    }
    for tag, key in (("MHSo", "ma_ho_so"), ("SBKe", "so_bang_ke"), ("NBKe", "ngay_bang_ke")):
        if _t(tc, tag):
            out[key] = _t(tc, tag)
    for tag, key in (("HDCTTChinh", "cho_thue_tai_chinh"), ("HDDCKPTQuan", "khu_phi_thue_quan"),
                     ("HDNTGia", "nhieu_ty_gia")):
        if _t(tc, tag):
            out[key] = _t(tc, tag) == "1"
    if _t(tc, "MSTDVNUNLHDon"):
        out["uy_nhiem"] = {"mst": _t(tc, "MSTDVNUNLHDon"), "ten": _t(tc, "TDVNUNLHDon"),
                           "dia_chi": _t(tc, "DCDVNUNLHDon")}
    lq = _child(tc, "TTHDLQuan")
    if lq is not None:
        tchd = ctx.int_(lq, "TCHDon")
        loai = ctx.int_(lq, "LHDCLQuan")
        ly_do = ctx.int_(lq, "LDDCTThe")
        out["hoa_don_lien_quan"] = {
            "tinh_chat": tchd, "tinh_chat_ten": TINH_CHAT_HOA_DON.get(tchd, ""),
            "mst": _t(lq, "MSTCLQuan"),
            "loai_hd": loai, "loai_hd_ten": LOAI_HD_LIEN_QUAN.get(loai, ""),
            "ky_hieu_mau": _t(lq, "KHMSHDCLQuan"), "ky_hieu": _t(lq, "KHHDCLQuan"),
            "so_hd": _t(lq, "SHDCLQuan"), "ngay_lap": _t(lq, "NLHDCLQuan"),
            "ly_do": ly_do, "ly_do_ten": LY_DO_DIEU_CHINH.get(ly_do, ""),
            "ghi_chu": _t(lq, "GChu"),
            "so_bang_ke": _t(lq, "SBKCLQuan"), "ngay_bang_ke": _t(lq, "NBKCLQuan"),
        }
    return out


def _parse_hhdvu(h, ctx: _Ctx) -> dict:
    line_khac = _ttkhac(h)
    tchat = ctx.int_(h, "TChat")
    thanh_tien = ctx.num(h, "ThTien")
    # Thẻ TThue theo dòng chỉ có ở HĐ kiêm tờ khai hoàn thuế; các TCGP thường đưa
    # tiền thuế dòng vào TTKhac (VATAmount).
    tien_thue = ctx.num(h, "TThue")
    if tien_thue is None and line_khac.get("VATAmount"):
        try:
            d = Decimal(line_khac["VATAmount"].strip())
            tien_thue = int(d) if d == d.to_integral_value() else float(d)
        except InvalidOperation:
            pass
    out = {
        "stt": ctx.int_(h, "STT"),
        "tinh_chat": tchat,
        "tinh_chat_ten": TINH_CHAT.get(tchat, ""),
        "ma_hhdv": _t(h, "MHHDVu"),
        "ten_hhdv": _t(h, "THHDVu"),
        "don_vi_tinh": _t(h, "DVTinh"),
        "so_luong": ctx.num(h, "SLuong"),
        "don_gia": ctx.num(h, "DGia"),
        "tl_ck": ctx.num(h, "TLCKhau"),
        "st_ck": ctx.num(h, "STCKhau"),
        "thanh_tien": thanh_tien,
        "thue_suat": _t(h, "TSuat"),
        "tien_thue": tien_thue,
        "tong_tien": (thanh_tien + tien_thue
                      if thanh_tien is not None and tien_thue is not None else None),
    }
    for tag, key in (("MNNNghe", "ma_nhom_nganh"), ("THang", "ten_hang")):
        if _t(h, tag):
            out[key] = _t(h, tag)
    for tag, key in (("DVTTe", "don_vi_tt"), ("TGia", "ty_gia"), ("TTDHoan", "tien_thue_duoc_hoan")):
        if _child(h, tag) is not None:
            out[key] = _t(h, tag) if tag == "DVTTe" else ctx.num(h, tag)
    dac_trung = [{"loai": ctx.int_(tin, "LHHDTrung"), "truong": _t(tin, "TTruong"),
                  "du_lieu": _t(tin, "DLieu")}
                 for tin in _children(_child(h, "TTHHDTrung"), "TTin")]
    if dac_trung:
        out["hang_dac_trung"] = dac_trung
    if line_khac:
        out["tt_khac"] = line_khac
    return out


def _parse_ttoan(tt, ctx: _Ctx) -> dict:
    out = {
        "tien_chua_thue": ctx.num(tt, "TgTCThue"),
        "tien_thue": ctx.num(tt, "TgTThue"),
        "chiet_khau": ctx.num(tt, "TTCKTMai"),
        "tong_tien_tt": ctx.num(tt, "TgTTTBSo"),
        "bang_chu": _t(tt, "TgTTTBChu"),
        "theo_thue_suat": [
            {"thue_suat": _t(l, "TSuat"), "thanh_tien": ctx.num(l, "ThTien"),
             "tien_thue": ctx.num(l, "TThue")}
            for l in _children(_child(tt, "THTTLTSuat"), "LTSuat")
        ],
    }
    for tag, key in (("TGTKCThue", "giam_tru_khong_chiu_thue"), ("TGTKhac", "giam_tru_khac"),
                     ("TgTDHoan", "tien_thue_duoc_hoan")):
        if _child(tt, tag) is not None:
            out[key] = ctx.num(tt, tag)
    phi = [{"ten": _t(p, "TLPhi"), "tien": ctx.num(p, "TPhi")}
           for p in _children(_child(tt, "DSLPhi"), "LPhi")]
    if phi:
        out["phi"] = phi
    return out


def _parse_dscks(ds) -> dict:
    """Thời điểm ký (SigningTime) của từng bên có chữ ký."""
    out = {}
    for party, key in (("NBan", "nguoi_ban"), ("NMua", "nguoi_mua"), ("CQT", "cqt")):
        el = _child(ds, party)
        if el is None:
            continue
        st = next((e.text for e in el.iter() if _ln(e.tag) == "SigningTime" and e.text), None)
        if st:
            out[key] = st.strip()
    return out


def _parse_hd_giay(g) -> dict:
    tc = _child(g, "TTChung")
    return {"ky_hieu_mau": _t(tc, "KHMSHDon"), "ky_hieu": _t(tc, "KHHDon"),
            "so_hd": _t(tc, "SHDon"), "ngay_lap": _t(tc, "NLap")}


def _invoice_level_ttkhac(dl) -> dict:
    """
    Gộp TTKhac/TTin cấp hóa đơn trong DLHDon → {TTruong: DLieu}.
    Bỏ TTin của dòng hàng (HHDVu) và hàng hóa đặc trưng — đó là dữ liệu của dòng.
    """
    if dl is None:
        return {}
    skip = set()
    for h in dl.iter():
        if _ln(h.tag) == "HHDVu":
            skip.update(id(e) for e in h.iter())
    out = {}
    for e in dl.iter():
        if _ln(e.tag) == "TTin" and id(e) not in skip:
            name = _t(e, "TTruong")
            if name and name not in out:
                out[name] = _t(e, "DLieu")
    return out


# ---------------------------------------------------------------------------
# QR Code (Phần I, Mục IV, Khoản 7): chuỗi TLV — ID 2 số, độ dài 2 số, giá trị
# ---------------------------------------------------------------------------

_QR_TRA_CUU = {"00": "guid", "01": "mst", "02": "ky_hieu_mau", "03": "ky_hieu",
               "04": "so_hd", "05": "ngay_lap", "06": "tong_tien_tt"}


def _parse_tlv(s: str):
    out, i = {}, 0
    while i < len(s):
        tid, ln = s[i:i + 2], s[i + 2:i + 4]
        if len(tid) < 2 or not tid.isdigit() or not ln.isdigit():
            return None
        n = int(ln)
        val = s[i + 4:i + 4 + n]
        if len(val) < n:
            return None
        out[tid] = val
        i += 4 + n
    return out


def _crc16_ccitt(data: str) -> str:
    """CRC-16/CCITT-FALSE (EMV QR): init 0xFFFF, poly 0x1021."""
    crc = 0xFFFF
    for b in data.encode("utf-8"):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def parse_qr(raw: str):
    """
    DLQRCode → {"raw", "tra_cuu": {...} | None, "crc_hop_le": bool | None}.
    Không có QR → None. Chuỗi không đúng TLV → chỉ trả "raw".
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    out = {"raw": raw, "tra_cuu": None, "crc_hop_le": None}
    tlv = _parse_tlv(raw)
    if tlv is None:
        return out
    if "63" in tlv:
        pos = raw.rfind("6304")
        out["crc_hop_le"] = pos >= 0 and _crc16_ccitt(raw[:pos + 4]) == tlv["63"].upper()
    sub = _parse_tlv(tlv.get("99", "")) if tlv.get("99") else None
    if sub:
        info = {key: sub[tid] for tid, key in _QR_TRA_CUU.items() if sub.get(tid)}
        d = info.get("ngay_lap", "")
        if len(d) == 8 and d.isdigit():
            info["ngay_lap"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        out["tra_cuu"] = info or None
    return out


# ---------------------------------------------------------------------------
# Kiểm tra so với quy định (cảnh báo, không chặn)
# ---------------------------------------------------------------------------

def _validate(r: dict, dl, ttoan) -> list:
    w = []
    tc, nb = r["thong_tin_chung"], r["nguoi_ban"]
    if dl is None:
        return ["Thiếu thẻ DLHDon"]
    for key, tag in (("ky_hieu_mau", "KHMSHDon"), ("ky_hieu", "KHHDon"), ("ngay_lap", "NLap")):
        if not tc.get(key):
            w.append(f"Thiếu {tag} (bắt buộc)")
    if not nb.get("ten") or not nb.get("mst"):
        w.append("Thiếu tên/MST người bán (bắt buộc)")
    elif not _MST_RE.match(nb["mst"]):
        w.append(f"MST người bán sai định dạng: {nb['mst']!r}")
    if tc.get("phien_ban") and tc["phien_ban"] not in PHIEN_BAN_DA_BIET:
        w.append(f"Phiên bản dữ liệu lạ: {tc['phien_ban']}")
    if tc.get("ky_hieu_mau") and tc["ky_hieu_mau"] not in KY_HIEU_MAU_SO:
        w.append(f"Ký hiệu mẫu số không thuộc Phụ lục II: {tc['ky_hieu_mau']!r}")

    ma = r["ma_cqt"]
    if tc.get("co_ma") and not ma:
        w.append("Hóa đơn có mã (ký hiệu bắt đầu bằng C) nhưng thiếu MCCQT")
    if ma and len(ma) != 34 and not _MCCQT_MTT_RE.match(ma):
        w.append(f"MCCQT sai định dạng (cần 34 ký tự, hoặc M#-YY-XXXXX-########### với MTT): {ma!r}")

    tt = r["thanh_toan"]
    if ttoan is not None:
        if tt.get("tong_tien_tt") is None:
            w.append("Thiếu TgTTTBSo (tổng tiền thanh toán, bắt buộc)")
        lines = tt.get("theo_thue_suat") or []
        for key, tag in (("thanh_tien", "TgTCThue"), ("tien_thue", "TgTThue")):
            total = tt.get("tien_chua_thue" if key == "thanh_tien" else "tien_thue")
            vals = [x[key] for x in lines if x.get(key) is not None]
            if total is not None and vals and abs(sum(vals) - total) > len(vals):
                w.append(f"Tổng theo thuế suất ({sum(vals)}) lệch {tag} ({total})")

    qr = r["qr"]
    if qr and qr.get("crc_hop_le") is False:
        w.append("QR Code sai mã kiểm tra CRC")
    if qr and qr.get("tra_cuu"):
        q = qr["tra_cuu"]
        if q.get("so_hd") and str(tc.get("so_hd")) != q["so_hd"].lstrip("0"):
            w.append(f"Số HĐ trong QR ({q['so_hd']}) khác SHDon ({tc.get('so_hd')})")
    return w


# ---------------------------------------------------------------------------
# Thông tin tra cứu bản gốc (cho bước tải PDF)
# ---------------------------------------------------------------------------

# Mỗi đơn vị cung cấp HĐĐT đặt tên trường mã tra cứu khác nhau trong TTKhac
# (QĐ 1233 không quy định trường này). Thứ tự ưu tiên lấy từ XML thật (10/2026):
# MTCuu, SearchKey/Fkey, TransactionID, QuanLy_SoBaoMat. Đây chỉ là giá trị "đoán" —
# adapter PDF của từng đơn vị đọc thẳng tt_khac theo đúng trường mình cần.
_MA_TRA_CUU_FIELDS = (
    "MTCuu", "MaTraCuu", "MaTC", "SearchKey", "TransactionID",
    "QuanLy_SoBaoMat", "SoBaoMat", "MaBaoMat", "LookupCode", "Fkey",
)

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_IGNORE = ("w3.org", "example.org", "xmlsoap.org", "uri.etsi.org")


def _guess_ma_tra_cuu(tt_khac: dict) -> str:
    for field in _MA_TRA_CUU_FIELDS:
        val = (tt_khac.get(field) or "").strip()
        if val:
            return val
    return ""


def _find_lookup_url(xml_str: str) -> str:
    for m in _URL_RE.finditer(xml_str):
        url = m.group(0)
        if not any(bad in url for bad in _URL_IGNORE):
            return url
    return ""
