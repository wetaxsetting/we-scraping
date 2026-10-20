# pdf_providers/render.py
"""
Dựng PDF từ HTML hóa đơn bằng Chromium (Playwright) — cho cổng tra cứu KHÔNG có PDF
trên server (VNPT: nút "Tải file pdf" chạy html2pdf.js ngay trên trình duyệt).

An toàn: tắt JavaScript, chặn mọi request mạng (HTML xem trước tự chứa ảnh dạng data:),
nên trang của bên thứ ba không chạy được gì trên máy mình.
"""
from __future__ import annotations

from .base import PdfFetchError

_PAGE = ('<!doctype html><html><head><meta charset="utf-8"><base href="{base}">'
         '<style>body{{margin:0}}</style></head><body>{body}</body></html>')


def html_to_pdf(body: str, base: str) -> bytes:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise PdfFetchError("Thiếu playwright (pip install playwright && playwright install chromium)",
                            "other") from e
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                ctx = browser.new_context(java_script_enabled=False)
                ctx.route("**/*", lambda route: route.abort())
                page = ctx.new_page()
                page.set_content(_PAGE.format(base=base, body=body), wait_until="load")
                return page.pdf(format="A4", print_background=True,
                                margin={"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"})
            finally:
                browser.close()
    except PdfFetchError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PdfFetchError(f"Chromium không dựng được PDF: {e}", "other") from e
