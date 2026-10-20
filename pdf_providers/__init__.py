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
