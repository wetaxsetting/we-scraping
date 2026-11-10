"""PDF "bản của Thuế": dựng từ trang xem hóa đơn có sẵn trong ZIP export-xml của GDT.

ZIP GDT trả về KHÔNG có PDF (đo 10/11/2026 trên 4.402 ZIP: luôn là `invoice.xml`, `invoice.html`,
`details.js`, `viewinvoice-bg.jpg`, `sign-check.jpg`). `invoice.html` là đúng trang "Xem hóa đơn" của
cổng Thuế; `details.js` vẽ mã QR và khung chữ ký. Ta mở trang đó bằng Chromium rồi in ra PDF theo
CSS in của chính GDT (A4, lề 0; hóa đơn dài thì 2 trang như khi in trên cổng).

Dùng làm bản tạm cho tới khi có PDF gốc của NCC (`invoice_pdf`); backend chỉ hiện bản này khi
chưa có PDF gốc.

An toàn: chỉ giải nén đúng 5 tên tệp phẳng (không đường dẫn con) vào thư mục tạm, chặn mọi request
không phải `file:` (trang không gọi được mạng), giới hạn kích thước.
"""
from __future__ import annotations

import atexit
import io
import logging
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

PAGE = "invoice.html"
ALLOWED = {"invoice.html", "details.js", "viewinvoice-bg.jpg", "sign-check.jpg"}
MAX_ENTRY = 5 * 1024 * 1024
TIMEOUT_MS = 30_000


class GdtPdfError(Exception):
    pass


def enabled() -> bool:
    """INVOICE_GDT_PDF=0 để tắt (mặc định bật)."""
    return os.getenv("INVOICE_GDT_PDF", "1").strip().lower() not in ("0", "false", "no", "off")


def has_view(zip_bytes: bytes) -> bool:
    try:
        return PAGE in zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
    except zipfile.BadZipFile:
        return False


def _extract(zip_bytes: bytes, dest: Path) -> Path:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise GdtPdfError(f"ZIP hỏng: {e}") from e
    for info in zf.infolist():
        if info.filename not in ALLOWED:          # chỉ tên phẳng đã biết → không thoát khỏi thư mục tạm
            continue
        if info.file_size > MAX_ENTRY:
            raise GdtPdfError(f"{info.filename} quá lớn ({info.file_size} byte)")
        (dest / info.filename).write_bytes(zf.read(info))
    page = dest / PAGE
    if not page.exists():
        raise GdtPdfError("ZIP không có invoice.html")
    return page


class _Renderer:
    """1 Chromium dùng lại cho cả tiến trình (mở mỗi HĐ 1 lần thì chậm gấp ~3)."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._thread = None

    def _ensure(self):
        if self._browser is not None and self._thread == threading.get_ident() and self._browser.is_connected():
            return self._browser
        self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise GdtPdfError("Thiếu playwright (pip install playwright && playwright install chromium)") from e
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._thread = threading.get_ident()
        return self._browser

    def render(self, page_path: Path) -> bytes:
        browser = self._ensure()
        ctx = browser.new_context()
        try:
            ctx.route("**/*", lambda r: r.continue_() if r.request.url.startswith("file:") else r.abort())
            page = ctx.new_page()
            page.set_default_timeout(TIMEOUT_MS)
            page.goto(page_path.as_uri(), wait_until="load")
            page.emulate_media(media="print")
            return page.pdf(prefer_css_page_size=True, print_background=True)
        finally:
            ctx.close()

    def close(self):
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001 — đóng muộn lúc thoát, không quan trọng
            pass
        self._pw = self._browser = self._thread = None


_renderer = _Renderer()
atexit.register(_renderer.close)


def render(zip_bytes: bytes) -> bytes:
    """ZIP export-xml của GDT → PDF (bytes). Lỗi → GdtPdfError."""
    tmp = Path(tempfile.mkdtemp(prefix="gdtpdf_"))
    try:
        page = _extract(zip_bytes, tmp)
        try:
            pdf = _renderer.render(page)
        except GdtPdfError:
            raise
        except Exception as e:  # noqa: BLE001
            _renderer.close()   # Chromium hỏng → lần sau mở lại
            raise GdtPdfError(f"Chromium không dựng được PDF: {e}") from e
        if not pdf.startswith(b"%PDF-"):
            raise GdtPdfError("Kết quả không phải PDF")
        return pdf
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
