"""
Đọc captcha bằng ddddocr (model OCR tổng quát, không phải model của ta).

Dùng cho site có captcha "dễ" (chữ rõ, nền nhiễu đơn giản) mà ta chưa có nhóm model nào
đọc được — đo trên site thật thì ddddocr (kèm tiền xử lý của từng adapter) đã đủ để server
nhận phần lớn số lần, adapter chỉ cần vòng thử lại vài captcha.

KHÔNG dùng cho site đã có nhóm model riêng: ở đó dùng common.solve_captcha (ép nhóm).
Lưu ý: ddddocr 1.6 `set_ranges` làm hỏng kết quả (đọc thiếu ký tự) → không dùng, adapter tự
lọc ký tự sau khi đọc.
"""
from __future__ import annotations

from .base import PdfFetchError

_OCR = None


def ocr_text(img: bytes) -> str:
    """Chuỗi ddddocr đọc được (chưa lọc). Nạp model 1 lần cho cả tiến trình."""
    global _OCR
    if _OCR is None:
        try:
            import ddddocr
        except ImportError as e:
            raise PdfFetchError("Thiếu ddddocr (pip install ddddocr) để đọc captcha", "other") from e
        _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR.classification(img) or ""
