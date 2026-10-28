# pdf_providers/__init__.py
"""
Registry adapter tải PDF, tra theo source_id (einvoice_providers.source_id).

Thêm 1 đơn vị cung cấp HĐĐT:
    1. Viết adapter trong pdf_providers/<ten>.py, kế thừa PdfAdapter, gọi register().
    2. Import module đó ở cuối file này.
    3. Gắn MSTTCGP với adapter:
           python web/invoice_job.py set-provider --mst-tcgp 0105987432 --source-id <source_id>
       → các HĐ đang ở 'no_source' của đơn vị đó tự quay lại hàng đợi.
"""
from __future__ import annotations

from .base import PdfAdapter, PdfFetchError

_REGISTRY: dict = {}


def register(adapter: PdfAdapter) -> PdfAdapter:
    if not adapter.source_id:
        raise ValueError("adapter thiếu source_id")
    _REGISTRY[adapter.source_id] = adapter
    return adapter


def get_adapter(source_id: str | None) -> PdfAdapter | None:
    return _REGISTRY.get(source_id) if source_id else None


def available() -> list:
    return sorted(_REGISTRY)


__all__ = ["PdfAdapter", "PdfFetchError", "register", "get_adapter", "available"]

# Adapter cụ thể: import ở đây để tự đăng ký.
from . import misa  # noqa: E402,F401  MISA meInvoice (0101243150)
from . import easyinvoice  # noqa: E402,F401  EasyInvoice / Softdreams (0105987432)
from . import vnpt  # noqa: E402,F401  VNPT Invoice (0100684378) — cần playwright + chromium
from . import viettel  # noqa: E402,F401  Viettel S-Invoice (0100109106)
from . import hilo  # noqa: E402,F401  T-VAN HILO (0106713804) — Grab; cần ddddocr
from . import vetc  # noqa: E402,F401  VETC (0107500414; 0106858609 bãi đỗ: chưa có cổng)
from . import minvoice  # noqa: E402,F401  M-INVOICE (0106026495, cả chi nhánh -001)
from . import myinvoice  # noqa: E402,F401  My Software / MyInvoice (0108971656) — tra bằng MCCQT
from . import visnam  # noqa: E402,F401  Visnam vin-hoadon (0401486901) — captcha g_1500x350
from . import bkav  # noqa: E402,F401  BKAV eHoadon (0101360697) — tra bằng DLHDon/@Id
from . import ipos  # noqa: E402,F401  iPOS (0104789847) — PDF + XML qua link trong mail, không captcha
from . import thaco  # noqa: E402,F401  THACO (3600252847) — link DownloadPdf trong TTKhac, không captcha
from . import vinhhy  # noqa: E402,F401  Vĩnh Hy ehoadondientu.com (0314743623) — tra bằng DLHDon/@Id, không captcha
from . import minhkhang  # noqa: E402,F401  Minh Khang hoadondientuvat.com (0314209362) — cùng nền tảng Vĩnh Hy
from . import xcyber  # noqa: E402,F401  CyberLotus xcyber/cyberbill (0105232093) — captcha g_300x75
from . import thaison  # noqa: E402,F401  Thái Sơn einvoice.vn / einvoice.mmvietnam.com (0101300842) — captcha g_200x70
from . import smartsign  # noqa: E402,F401  SmartSign / Vi Na (0309612872) — captcha bằng ddddocr
from . import pavietnam  # noqa: E402,F401  P.A Việt Nam hoadon.pavietnam.vn (0302431595) — mã chỉ có trong mail
from . import vnisc  # noqa: E402,F401  VNISC invoice.abcsys.vn (0102331762) — captcha bằng ddddocr
from . import fast  # noqa: E402,F401  FAST einvoice.fast.com.vn (0100727825) — TTKhac KeySearch; bước tải không kiểm captcha
from . import express247  # noqa: E402,F401  247Express hoadon.247express.vn (người bán 0304043037) — mã từ link mail, không captcha
from . import evat  # noqa: E402,F401  eVAT *.evat.vn — Phương Nam (0316642395); TTKhac "Mã tra cứu hóa đơn"
from . import matbao  # noqa: E402,F401  Mắt Bão matbao.in (0302712571) — Fkey suy từ XML qua API của cổng
from . import onlinevina  # noqa: E402,F401  Online VI NA (0314185087) — TTKhac SearchInvoice; dựng PDF từ XML + XSLT (cần lxml)
from . import vininvoice  # noqa: E402,F401  VININVOICE tracuu.vininvoice.vn (0109282176) — MCCQT, không captcha
from . import wininvoice  # noqa: E402,F401  WinInvoice tracuu.wininvoice.vn (0312303803) — cùng phần mềm eVAT
from . import rosysoft  # noqa: E402,F401  Rosy Soft einv.rosysoft.vn (0305142231) — Vinasun/Ánh Dương; không captcha
from . import asiainvoice  # noqa: E402,F401  ASIA asiainvoice.vn (0101162173) — FKey chỉ có trong mail; captcha ddddocr
from . import fahasa  # noqa: E402,F401  Fahasa hoadon.fahasa.com (FPT tự host) — chỉ qua link mail, không nhận MSTTCGP FPT
from . import mobifone  # noqa: E402,F401  MobiFone tracuuhoadon(vienthong).mobifone.vn (0100686209) — số bảo mật từ mail, không captcha; PDF + XML
# 27/10/2026: NCC trong docs/Danh_sach_NCC.xlsx chưa có HĐ thật — chỉ kiểm phản hồi "không tìm thấy"
from . import tialuaviet  # noqa: E402,F401  Tia Lửa Việt (ihoadondientu.com) — phần mềm Vĩnh Hy
from . import tuanchau  # noqa: E402,F401  Tuần Châu (e-invoicetuanchau.com) — phần mềm Vĩnh Hy
from . import minhthu  # noqa: E402,F401  Minh Thư Vũng Tàu (hoadonminhthuvungtau.com) — phần mềm Vĩnh Hy
from . import htsaigon  # noqa: E402,F401  HT Sài Gòn (ihoadondientu.net) — phần mềm Vĩnh Hy
from . import lienson  # noqa: E402,F401  Liên Sơn (ls-invoiceapp.lienson.vn) — nền tảng xcyber, g_300x75
from . import myinvoicevn  # noqa: E402,F401  My-Invoice 0106820789 (hoadondientuvn.info) — xcyber bill2app
from . import ptp  # noqa: E402,F401  PTP-Invoice (hoadondientu-ptp.vn) — MST bán + số bảo mật
from . import acman  # noqa: E402,F401  ACMan (hoadondientu.acman.vn) — form WebForms
from . import avse  # noqa: E402,F401  Avsoft-Einvoice (hoadondientu.link/tracuutt78)
from . import koffi  # noqa: E402,F401  Koffi/ZAMO (api.koffi.vn/invoices/code) — mã trong mail
from . import vietinfo  # noqa: E402,F401  VietInfo (tracuuhoadon.vietinfo.tech) — mã + ngày HĐ
from . import ehcm  # noqa: E402,F401  SaigonInvoice/eHCM (invoice.ehcm.vn) — form WebForms
from . import kiotviet  # noqa: E402,F401  KiotViet (tracuuhoadon.kiotviet.vn) — mã mtc từ link mail; thật 1/1
from . import homecasta  # noqa: E402,F401  Home Casta — cùng phần mềm VININVOICE
from . import sonphat  # noqa: E402,F401  Sơn Phát (hoadon.sonphat.vn) — API lookupCode
from . import atis  # noqa: E402,F401  ATIS — cùng phần mềm cổng VETC
from . import aits  # noqa: E402,F401  AITS — MST bán + Số bảo mật
from . import kimtuthap  # noqa: E402,F401  Kim Tự Tháp e-invoices.vn — mã bí mật GUID
from . import intrust  # noqa: E402,F401  INTRUST / SCHOOL TECH lookupeinvoice.intrustdss.vn — reCAPTCHA server không kiểm
from . import sapo  # noqa: E402,F401  Sapo Invoice invoice.sapo.vn — mã tra cứu, không captcha
from . import igas  # noqa: E402,F401  IGAS tracuu.igas.vn — phần mềm VIET-INVOICE, không captcha
from . import iam  # noqa: E402,F401  iam-entry: T-HOADON (0318511655) + IAM (0109193102) — MCCQT, không captcha
from . import sctv  # noqa: E402,F401  SCTV tracuu-hddt.sctv.vn — số bảo mật, không captcha
from . import mmhoadon  # noqa: E402,F401  MOMO E-INVOICE (0305289153) — mã tra cứu → id → PDF/XML
from . import gohoadon  # noqa: E402,F401  GO HÓA ĐƠN (0111245245) — lookupCode, không captcha
from . import epay  # noqa: E402,F401  EPAY (0108269133) — cổng M-invoice white-label, MST bán + số bảo mật
from . import atom  # noqa: E402,F401  ATOM 1invoice (0316887099) — như epay
from . import suninvoice  # noqa: E402,F401  SUN Invoice (0111488368) — cùng phần mềm myinvoice/vininvoice
from . import winvoice  # noqa: E402,F401  W-INVOICE Khánh Linh (0307633556) — Mã tra cứu hóa đơn
from . import vnpost  # noqa: E402,F401  VNPost (0102595740) — mã tra cứu; captcha chỉ ở trình duyệt
from . import megabiz  # noqa: E402,F401  Megabiz vinvoice (0105844836) — captcha g_120x38 (server nhận 10/10)
from . import pvoil  # noqa: E402,F401  PVOIL hoadon.pvoil.vn (0305795054) — captcha g_194x35 (10/10)
from . import cloudteam  # noqa: E402,F401  Nhóm Mây cloudteam.vn (0312617990) — captcha g_120x40_cloud (10/10)
from . import pmbk  # noqa: E402,F401  Bách Khoa hdbk / hoadonbachkhoa.pmbk.vn (0202029650, 0200784873) — mã tra cứu
from . import vietinvoice  # noqa: E402,F401  VIET-INVOICE / iCORP (0106870211) — 2 cổng: API white-label + /erp/rest như vininvoice
from . import wetax  # noqa: E402,F401  WeTax (api.wetax.com.vn) — chỉ link tải thẳng pdf/xml trong mail


def supported_mst_tcgp() -> list:
    """Các MSTTCGP đã có adapter mặc định."""
    return sorted({m for a in _REGISTRY.values() for m in getattr(a, "mst_tcgp", ())})


def adapter_for_host(host: str | None) -> PdfAdapter | None:
    """Adapter nhận link của cổng `host` (khớp tên miền hoặc subdomain của nó)."""
    host = (host or "").lower().strip(".")
    for adapter in _REGISTRY.values():
        for h in getattr(adapter, "hosts", ()):
            if host == h or host.endswith("." + h):
                return adapter
    return None


def adapter_for_mst_tcgp(mst_tcgp: str | None) -> PdfAdapter | None:
    """Adapter mặc định theo MSTTCGP (dùng khi einvoice_providers chưa gắn source_id).
    MST chi nhánh (0106026495-001) dùng adapter của công ty mẹ."""
    mst_tcgp = (mst_tcgp or "").strip()
    for key in dict.fromkeys((mst_tcgp, mst_tcgp.split("-")[0])):
        for adapter in _REGISTRY.values():
            if key and key in getattr(adapter, "mst_tcgp", ()):
                return adapter
    return None
