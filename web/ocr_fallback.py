# web/ocr_fallback.py
"""OCR fallback dùng ddddocr khi model confidence thấp."""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

import ddddocr
from PIL import Image

_OCR = None
CLEAN_RE = re.compile(r"[^a-zA-Z0-9]")


def _get_ocr() -> ddddocr.DdddOcr:
    global _OCR
    if _OCR is None:
        _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


def _flatten_white(img: Image.Image) -> Image.Image:
    """Dán ảnh RGBA lên nền trắng trước khi OCR."""
    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, img).convert("RGB")
    return img.convert("RGB")


def ocr_predict(img_bytes_or_path) -> Optional[str]:
    """
    Chạy OCR trên ảnh. Trả về chuỗi text hoặc None nếu fail.
    Chấp nhận: bytes, Path, str (path).
    """
    try:
        if isinstance(img_bytes_or_path, (str, Path)):
            with open(img_bytes_or_path, "rb") as f:
                raw = f.read()
        else:
            raw = img_bytes_or_path

        # Flatten alpha → trắng để OCR đọc rõ hơn
        with Image.open(io.BytesIO(raw)) as im:
            im_rgb = _flatten_white(im)
            buf = io.BytesIO()
            im_rgb.save(buf, format="PNG")
            clean_bytes = buf.getvalue()

        text = _get_ocr().classification(clean_bytes)
        text = CLEAN_RE.sub("", text or "")
        return text if text else None
    except Exception as e:
        print(f"[OCR] lỗi: {e}")
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(f"OCR: {ocr_predict(sys.argv[1])!r}")