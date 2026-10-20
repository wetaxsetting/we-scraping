# gdt_captcha_solver.py
"""
Auto-solve GDT captcha bằng CRNN model đã train.

Flow:
    1. GDT trả SVG captcha (200x40)
    2. Render SVG → PNG (dùng skia)
    3. Chạy CRNN Predictor → text + confidence
    4. Nếu confidence < ngưỡng → thử OCR fallback (ddddocr)
    5. Nếu cả 2 không chắc → trả về None (caller retry captcha mới)

Usage:
    from gdt_captcha_solver import GDTCaptchaSolver
    solver = GDTCaptchaSolver()
    text, conf = solver.solve(svg_string)
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Optional, Tuple

# ============================================================
# ⚠️ SỬA PATH NÀY CHO ĐÚNG MÁY BẠN
# ============================================================
SCAN_CAPTCHA_ROOT = Path(r"C:\Project\scan-captcha-project")
# ============================================================

if not SCAN_CAPTCHA_ROOT.exists():
    raise FileNotFoundError(
        f"Không tìm thấy scan-captcha-project tại: {SCAN_CAPTCHA_ROOT}\n"
        f"Sửa biến SCAN_CAPTCHA_ROOT ở đầu file."
    )

# Thêm root vào sys.path để import module `captcha`
if str(SCAN_CAPTCHA_ROOT) not in sys.path:
    sys.path.insert(0, str(SCAN_CAPTCHA_ROOT))

# Import sau khi đã thêm path
from captcha.config import Config, default_config_path
from captcha.predict import Predictor
from captcha.utils import get_device
from PIL import Image


class GDTCaptchaSolver:
    """
    Solver captcha GDT. Load model 1 lần, tái sử dụng cho nhiều request.
    """

    # Ngưỡng confidence — dưới ngưỡng này sẽ thử OCR fallback
    CONF_THRESHOLD = 0.80

    # Nếu cả 2 engine đều < ngưỡng này → trả None (caller nên retry)
    MIN_ACCEPTABLE = 0.50

    def __init__(
        self,
        model_root: Optional[Path] = None,
        backend: str = "torch",
        device: str = "cpu",
        enable_ocr: bool = True,
    ):
        self.root = Path(model_root) if model_root else SCAN_CAPTCHA_ROOT
        self.backend = backend
        self.device = get_device(device)

        # Load config (chứa thông tin 18-20 groups)
        cfg_path = default_config_path(self.root)
        if not cfg_path.exists():
            raise FileNotFoundError(f"Config không tồn tại: {cfg_path}")

        self.cfg = Config.load(cfg_path)
        self.predictor = Predictor(
            self.cfg, self.root, backend=backend, device=self.device,
        )

        # OCR fallback (ddddocr)
        self._ocr = None
        if enable_ocr:
            try:
                import ddddocr
                self._ocr = ddddocr.DdddOcr(show_ad=False)
            except ImportError:
                print("[solver] ddddocr chưa cài — bỏ qua OCR fallback")

    # --------------------------------------------------------
    # SVG → PNG
    # --------------------------------------------------------
    @staticmethod
    def _svg_to_png(svg_str: str, scale: float = 3.0) -> bytes:
        """Render SVG string → PNG bytes (nền trắng)."""
        import skia

        stream = skia.MemoryStream()
        stream.setMemory(svg_str.encode("utf-8"))
        skia_svg = skia.SVGDOM.MakeFromStream(stream)
        w, h = skia_svg.containerSize()
        if w <= 0 or h <= 0:
            raise ValueError(f"SVG invalid size {w}x{h}")

        surface = skia.Surface(int(w * scale), int(h * scale))
        with surface as canvas:
            canvas.clear(skia.ColorWHITE)
            canvas.scale(scale, scale)
            skia_svg.render(canvas)

        return bytes(surface.makeImageSnapshot().encodeToData())

    # --------------------------------------------------------
    # OCR fallback
    # --------------------------------------------------------
    def _ocr_predict(self, png_bytes: bytes) -> Optional[str]:
        if not self._ocr:
            return None
        try:
            text = self._ocr.classification(png_bytes)
            # Loại ký tự lạ
            import re
            text = re.sub(r"[^a-zA-Z0-9]", "", text or "")
            return text or None
        except Exception as e:
            print(f"[solver] OCR lỗi: {e}")
            return None

    # --------------------------------------------------------
    # Main solve
    # --------------------------------------------------------
    def solve(self, svg_str: str) -> Tuple[Optional[str], float]:
        """
        Giải captcha từ SVG string.
        Trả về (text, confidence).
        - text = None nếu không giải được (caller nên retry captcha mới)
        - confidence = 0.0 nếu fail
        """
        try:
            png_bytes = self._svg_to_png(svg_str, scale=3.0)
        except Exception as e:
            print(f"[solver] render SVG lỗi: {e}")
            return None, 0.0

        # Lưu tạm để Predictor đọc (nó cần path, không nhận bytes)
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "gdt_captcha_tmp.png"
        tmp.write_bytes(png_bytes)

        # Chạy CRNN
        try:
            pred = self.predictor.predict_path(tmp)
        except Exception as e:
            print(f"[solver] CRNN lỗi: {e}")
            return None, 0.0

        text_model = pred.text or ""
        conf_model = float(pred.confidence or 0.0)

        # Nếu model không tự tin → thử OCR
        if conf_model < self.CONF_THRESHOLD and self._ocr:
            text_ocr = self._ocr_predict(png_bytes)

            # Cả 2 đồng ý → tăng confidence
            if text_ocr and text_ocr == text_model:
                return text_model, min(1.0, conf_model + 0.15)

            # OCR tự tin hơn model → dùng OCR
            if text_ocr and len(text_ocr) == len(text_model):
                # Heuristic: nếu model conf thấp mà OCR ra text hợp lý
                if conf_model < 0.5:
                    return text_ocr, 0.65

        # Model tự tin → OK
        if conf_model >= self.MIN_ACCEPTABLE:
            return text_model, conf_model

        # Quá thấp → không dùng
        return None, conf_model


# ============================================================
# Test nhanh
# ============================================================
if __name__ == "__main__":
    import requests
    requests.packages.urllib3.disable_warnings()

    print("Testing GDT captcha solver...")

    solver = GDTCaptchaSolver()

    # Lấy 1 captcha mẫu từ GDT
    r = requests.get(
        "https://hoadondientu.gdt.gov.vn/api/captcha",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        timeout=15, verify=False,
    )
    data = r.json()
    svg = data.get("content", "")
    key = data.get("key", "")

    print(f"Got captcha key: {key}")
    print(f"SVG length: {len(svg)}")

    text, conf = solver.solve(svg)
    print(f"\n>>> Kết quả: text={text!r}  confidence={conf:.3f}")