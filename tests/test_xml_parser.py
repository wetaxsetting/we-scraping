"""xml_parser theo QĐ 1233/QĐ-CT (27/08/2026): cấu trúc thẻ, danh mục, QR, cảnh báo.

Parser sai ở đây không làm job lỗi — nó lưu số liệu sai vào DB một cách êm ái
(nhầm người bán với chữ ký số người bán, gộp TTKhac của dòng hàng vào hóa đơn,
bỏ qua hóa đơn điều chỉnh...). Nên kiểm theo đúng đường dẫn thẻ trong quy định.

Run::

    pytest tests/test_xml_parser.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import xml_parser  # noqa: E402
from xml_parser import _crc16_ccitt, parse_qr, parse_xml_bytes  # noqa: E402


def _tlv(tid: str, val: str) -> str:
    return f"{tid}{len(val):02d}{val}"


def _qr(sub: dict, with_crc=True) -> str:
    body = _tlv("00", "01") + _tlv("99", "".join(_tlv(k, v) for k, v in sub.items()))
    if not with_crc:
        return body
    body += "6304"
    return body + _crc16_ccitt(body)


# Hóa đơn GTGT điều chỉnh, 2 thuế suất, có phí, hàng hóa đặc trưng, TTKhac nhiều cấp.
QR_OK = _qr({"01": "0100112233", "02": "1", "03": "C26TAA", "04": "123",
             "05": "20261101", "06": "1188000"})
FULL = f"""<?xml version="1.0" encoding="UTF-8"?>
<HDon>
 <DLHDon Id="data">
  <TTChung>
   <PBan>2.1.1</PBan><THDon>Hóa đơn giá trị gia tăng</THDon><KHMSHDon>1</KHMSHDon>
   <KHHDon>C26TAA</KHHDon><SHDon>123</SHDon><NLap>2026-11-01</NLap>
   <DVTTe>VND</DVTTe><HTTToan>TM/CK</HTTToan><MSTTCGP>0105987432</MSTTCGP>
   <HDCTTChinh>0</HDCTTChinh>
   <TTHDLQuan>
    <TCHDon>2</TCHDon><LHDCLQuan>5</LHDCLQuan><KHMSHDCLQuan>1</KHMSHDCLQuan>
    <KHHDCLQuan>C26TAA</KHHDCLQuan><SHDCLQuan>100</SHDCLQuan><NLHDCLQuan>2026-10-20</NLHDCLQuan>
    <LDDCTThe>4</LDDCTThe><GChu>Chiết khấu quý</GChu>
   </TTHDLQuan>
   <TTKhac><TTin><TTruong>MTCuu</TTruong><KDLieu>string</KDLieu><DLieu>ABC123</DLieu></TTin></TTKhac>
  </TTChung>
  <NDHDon>
   <NBan>
    <Ten>NCC A</Ten><MST>0100112233</MST><DChi>Hà Nội</DChi><Website>a.vn</Website>
    <TTKhac><TTin><TTruong>Fkey</TTruong><DLieu>FK-9</DLieu></TTin></TTKhac>
   </NBan>
   <NMua><Ten>Cty B</Ten><MST>1201496252</MST><DChi>HCM</DChi><HVTNMHang>Nguyễn Văn A</HVTNMHang></NMua>
   <DSHHDVu>
    <HHDVu><TChat>1</TChat><STT>1</STT><THHDVu>Hàng A</THHDVu><DVTinh>Cái</DVTinh>
     <SLuong>2.5</SLuong><DGia>400000</DGia><ThTien>1000000</ThTien><TSuat>8%</TSuat>
     <TTKhac><TTin><TTruong>VATAmount</TTruong><DLieu>80000</DLieu></TTin>
             <TTin><TTruong>MTCuu</TTruong><DLieu>CUA-DONG-HANG</DLieu></TTin></TTKhac>
    </HHDVu>
    <HHDVu><TChat>5</TChat><STT>2</STT><THHDVu>Xăng</THHDVu><ThTien>100000</ThTien><TSuat>KCT</TSuat>
     <TTHHDTrung><TTin><LHHDTrung>1</LHHDTrung><TTruong>BKS</TTruong><DLieu>30A12345</DLieu></TTin></TTHHDTrung>
    </HHDVu>
    <HHDVu><TChat>4</TChat><THHDVu>Ghi chú: giao tại kho</THHDVu></HHDVu>
   </DSHHDVu>
   <TToan>
    <THTTLTSuat>
     <LTSuat><TSuat>8%</TSuat><ThTien>1000000</ThTien><TThue>80000</TThue></LTSuat>
     <LTSuat><TSuat>KCT</TSuat><ThTien>100000</ThTien><TThue>0</TThue></LTSuat>
    </THTTLTSuat>
    <TgTCThue>1100000</TgTCThue><TgTThue>80000</TgTThue>
    <DSLPhi><LPhi><TLPhi>Phí môi trường</TLPhi><TPhi>8000</TPhi></LPhi></DSLPhi>
    <TTCKTMai>0</TTCKTMai><TgTTTBSo>1188000</TgTTTBSo><TgTTTBChu>Một triệu...</TgTTTBChu>
   </TToan>
  </NDHDon>
 </DLHDon>
 <DSHDGiay><DLHDGiay><TTChung><KHMSHDon>01GTKT0/001</KHMSHDon><KHHDon>AA/19E</KHHDon>
  <SHDon>77</SHDon><NLap>2019-05-01</NLap>
  <TTKhac><TTin><TTruong>MTCuu</TTruong><DLieu>CUA-HD-GIAY</DLieu></TTin></TTKhac></TTChung></DLHDGiay></DSHDGiay>
 <DLQRCode>{QR_OK}</DLQRCode>
 <MCCQT>00AAA52F4AAE1234567890ABCDEF123456</MCCQT>
 <DSCKS>
  <NBan><Signature><Object><QualifyingProperties><SignedProperties><SignedSignatureProperties>
   <SigningTime>2026-11-01T09:00:00</SigningTime></SignedSignatureProperties></SignedProperties>
   </QualifyingProperties></Object><Ten>KHÔNG PHẢI TÊN NGƯỜI BÁN</Ten></Signature></NBan>
  <CQT><Signature><Object><SigningTime>2026-11-01T09:05:00</SigningTime></Object></Signature></CQT>
 </DSCKS>
</HDon>"""


@pytest.fixture(scope="module")
def full():
    return parse_xml_bytes(FULL.encode())


def test_thong_tin_chung(full):
    tc = full["thong_tin_chung"]
    assert tc["phien_ban"] == "2.1.1" and tc["ky_hieu_mau"] == "1"
    assert tc["ten_loai_mau"] == "Hóa đơn giá trị gia tăng"
    assert tc["so_hd"] == 123 and tc["ngay_lap"] == "2026-11-01"     # ngày trong XML là giờ VN
    assert tc["co_ma"] is True and tc["mst_tcgp"] == "0105987432"
    assert tc["cho_thue_tai_chinh"] is False


def test_hoa_don_dieu_chinh(full):
    lq = full["thong_tin_chung"]["hoa_don_lien_quan"]
    assert lq["tinh_chat"] == 2 and lq["tinh_chat_ten"] == "Điều chỉnh"
    assert lq["ly_do"] == 4 and lq["ly_do_ten"] == "Điều chỉnh do chiết khấu thương mại"
    assert (lq["ky_hieu"], lq["so_hd"], lq["ngay_lap"]) == ("C26TAA", "100", "2026-10-20")


def test_nguoi_ban_khong_lan_voi_chu_ky_so(full):
    # DSCKS cũng có thẻ NBan — parser phải lấy NDHDon/NBan
    nb = full["nguoi_ban"]
    assert nb["ten"] == "NCC A" and nb["mst"] == "0100112233" and nb["website"] == "a.vn"
    assert "lenh_dieu_dong" not in nb                                 # thẻ của phiếu xuất kho
    assert full["nguoi_mua"]["nguoi_mua_hang"] == "Nguyễn Văn A"


def test_hang_hoa(full):
    h1, h2, h3 = full["hang_hoa"]
    assert h1["so_luong"] == 2.5 and h1["don_gia"] == 400000 and isinstance(h1["thanh_tien"], int)
    assert h1["tien_thue"] == 80000 and h1["tong_tien"] == 1080000    # VATAmount từ TTKhac dòng
    assert h1["tt_khac"]["MTCuu"] == "CUA-DONG-HANG"
    assert h2["tinh_chat_ten"] == "Hàng hóa đặc trưng"
    assert h2["hang_dac_trung"] == [{"loai": 1, "truong": "BKS", "du_lieu": "30A12345"}]
    assert h3["tinh_chat"] == 4 and h3["thanh_tien"] is None and h3["tong_tien"] is None


def test_thanh_toan(full):
    tt = full["thanh_toan"]
    assert (tt["tien_chua_thue"], tt["tien_thue"], tt["tong_tien_tt"]) == (1100000, 80000, 1188000)
    assert [x["thue_suat"] for x in tt["theo_thue_suat"]] == ["8%", "KCT"]
    assert tt["phi"] == [{"ten": "Phí môi trường", "tien": 8000}]


def test_tt_khac_chi_cap_hoa_don(full):
    # TTKhac của dòng hàng và của hóa đơn giấy liên quan KHÔNG được gộp vào
    assert full["tt_khac"] == {"MTCuu": "ABC123", "Fkey": "FK-9"}
    assert full["ma_tra_cuu"] == "ABC123"
    assert full["hoa_don_giay"][0]["ky_hieu_mau"] == "01GTKT0/001"


def test_ma_cqt_ky_so_qr(full):
    assert len(full["ma_cqt"]) == 34
    assert full["ky_so"] == {"nguoi_ban": "2026-11-01T09:00:00", "cqt": "2026-11-01T09:05:00"}
    assert full["qr"]["crc_hop_le"] is True
    assert full["qr"]["tra_cuu"]["ngay_lap"] == "2026-11-01"
    assert full["qr"]["tra_cuu"]["so_hd"] == "123"


def test_hoa_don_hop_le_khong_co_canh_bao(full):
    assert full["canh_bao"] == []


# ---------------------------------------------------------------------------
# Cảnh báo
# ---------------------------------------------------------------------------

def _mini(ttchung="", nban="<Ten>A</Ten><MST>0100112233</MST>", ttoan="", tail=""):
    return f"""<HDon><DLHDon><TTChung><KHMSHDon>1</KHMSHDon><NLap>2026-11-01</NLap>{ttchung}</TTChung>
      <NDHDon><NBan>{nban}</NBan><NMua/>{ttoan}</NDHDon></DLHDon>{tail}</HDon>""".encode()


def test_canh_bao_hd_co_ma_thieu_mccqt():
    d = parse_xml_bytes(_mini("<KHHDon>C26TAA</KHHDon>"))
    assert any("thiếu MCCQT" in w for w in d["canh_bao"])
    d = parse_xml_bytes(_mini("<KHHDon>K26TAA</KHHDon>"))
    assert d["thong_tin_chung"]["co_ma"] is False and d["canh_bao"] == []


def test_canh_bao_tong_lech_va_so_hong():
    ttoan = """<TToan><THTTLTSuat><LTSuat><TSuat>8%</TSuat><ThTien>100</ThTien><TThue>8</TThue></LTSuat>
               </THTTLTSuat><TgTCThue>500</TgTCThue><TgTThue>8</TgTThue><TgTTTBSo>abc</TgTTTBSo></TToan>"""
    w = parse_xml_bytes(_mini("<KHHDon>K26TAA</KHHDon>", ttoan=ttoan))["canh_bao"]
    assert any("lệch TgTCThue" in x for x in w)
    assert any("TgTTTBSo không phải số" in x for x in w)
    assert not any("lệch TgTThue" in x for x in w)


def test_canh_bao_thieu_the_bat_buoc():
    w = parse_xml_bytes(_mini(nban="<Ten>A</Ten>"))["canh_bao"]
    assert "Thiếu KHHDon (bắt buộc)" in w
    assert "Thiếu tên/MST người bán (bắt buộc)" in w


def test_mccqt_may_tinh_tien_hop_le():
    d = parse_xml_bytes(_mini("<KHHDon>C26MAA</KHHDon>", tail="<MCCQT>M1-26-A1B2C-00001234567</MCCQT>"))
    assert d["canh_bao"] == []


# ---------------------------------------------------------------------------
# Phiếu xuất kho, namespace, thẻ gốc
# ---------------------------------------------------------------------------

def test_phieu_xuat_kho_fields():
    nban = ("<Ten>A</Ten><MST>0100112233</MST><LDDNBo>LD-01</LDDNBo>"
            "<TNVChuyen>Trần B</TNVChuyen><PTVChuyen>29C-12345</PTVChuyen>")
    d = parse_xml_bytes(_mini("<KHHDon>K26NAA</KHHDon>", nban=nban))
    assert d["nguoi_ban"]["lenh_dieu_dong"] == "LD-01"
    assert d["nguoi_ban"]["phuong_tien_van_chuyen"] == "29C-12345"


def test_namespace_va_the_goc_bao_ngoai():
    xml = ('<TDiep><DLieu><HDon xmlns="urn:x"><DLHDon><TTChung><KHMSHDon>2</KHMSHDon>'
           '<KHHDon>K26TAA</KHHDon><NLap>2026-11-01</NLap></TTChung><NDHDon><NBan><Ten>A</Ten>'
           '<MST>0100112233</MST></NBan></NDHDon></DLHDon></HDon></DLieu></TDiep>')
    d = parse_xml_bytes(xml.encode())
    assert d["thong_tin_chung"]["ten_loai_mau"] == "Hóa đơn bán hàng"
    assert d["nguoi_ban"]["mst"] == "0100112233"


def test_khong_co_hdon():
    with pytest.raises(ValueError):
        parse_xml_bytes(b"<Khac/>")


# ---------------------------------------------------------------------------
# QR Code
# ---------------------------------------------------------------------------

def test_crc16_ccitt_vector_chuan():
    assert _crc16_ccitt("123456789") == "29B1"       # giá trị kiểm tra chuẩn CRC-16/CCITT-FALSE


def test_qr_crc_sai_va_chuoi_khong_phai_tlv():
    tampered = QR_OK[:-1] + ("0" if QR_OK[-1] != "0" else "1")
    assert parse_qr(tampered)["crc_hop_le"] is False
    assert parse_qr("abc") == {"raw": "abc", "tra_cuu": None, "crc_hop_le": None}
    assert parse_qr("") is None


def test_qr_chap_nhan_truong_rong_va_thieu_crc():
    # Dữ liệu thật: có trường độ dài 00, có chuỗi không kèm CRC (63)
    raw = "000201" + "0200" + _tlv("99", _tlv("00", "") + _tlv("04", "00238805"))
    q = parse_qr(raw)
    assert q["crc_hop_le"] is None and q["tra_cuu"] == {"so_hd": "00238805"}
