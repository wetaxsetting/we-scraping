# pdf_providers/common.py
"""
Tiện ích dùng chung cho adapter tải PDF: phiên HTTP, giải captcha, kiểm tra PDF.

Giải captcha LUÔN ép đúng nhóm model của site (enforce_group). Router mặc định
chọn nhóm theo kích thước ảnh và đọc tự tin cả captcha của site nó chưa từng thấy —
sai mà không báo. Mỗi adapter biết site của mình dùng nhóm nào, nên khai báo thẳng.
"""
from __future__ import annotations

import io
import logging
import ssl
import sys
import tempfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from .base import PdfFetchError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}

TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _LegacySSLAdapter(HTTPAdapter):
    """Nhiều site tra cứu HĐĐT còn dùng cipher/chứng chỉ cũ."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        except ssl.SSLError:
            pass
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def new_session() -> requests.Session:
    """Phiên mới cho MỖI lần tra cứu (captcha thường gắn với cookie phiên)."""
    requests.packages.urllib3.disable_warnings()
    s = requests.Session()
    s.verify = False
    s.mount("https://", _LegacySSLAdapter())
    s.headers.update(BROWSER_HEADERS)
    return s


def request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Gọi HTTP, quy lỗi mạng / 5xx / 429 thành PdfFetchError có kind."""
    kwargs.setdefault("timeout", TIMEOUT)
    try:
        r = session.request(method, url, **kwargs)
    except requests.Timeout as e:
        raise PdfFetchError(f"Timeout {url}: {e}", "network") from e
    except requests.RequestException as e:
        raise PdfFetchError(f"Lỗi kết nối {url}: {e}", "network") from e
    if r.status_code == 429:
        raise PdfFetchError(f"429 từ {url}", "rate_limit")
    if r.status_code >= 500:
        raise PdfFetchError(f"HTTP {r.status_code} từ {url}", "server")
    return r


def link_params(url: str) -> dict:
    """Tham số query của link (khóa chữ thường). Chỉ phân tích chuỗi — không mở link."""
    from urllib.parse import parse_qsl, urlsplit
    return {k.lower(): v.strip() for k, v in parse_qsl(urlsplit(url).query) if v.strip()}


def is_pdf(data: bytes | None) -> bool:
    return bool(data) and data[:5] == b"%PDF-" and len(data) > 1000


def check_pdf_is_invoice(pdf: bytes, invoice: dict) -> None:
    """
    PDF phải nhắc đúng số hóa đơn — chặn trường hợp site trả PDF của HĐ khác
    (mã tra cứu trùng/nhầm). PDF không trích được chữ (bản scan) thì bỏ qua kiểm tra.
    """
    shdon = str(invoice.get("shdon") or "").strip()
    if not shdon:
        return
    try:
        import fitz
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            text = "".join(page.get_text() for page in doc)
    except Exception as e:  # noqa: BLE001
        raise PdfFetchError(f"PDF hỏng, không mở được: {e}", "bad_content") from e
    compact = "".join(text.split())
    if compact and shdon not in compact:
        raise PdfFetchError(f"PDF tải về không chứa số hóa đơn {shdon}", "bad_content")


# ---------------------------------------------------------------------------
# Captcha
# ---------------------------------------------------------------------------

_PREDICTORS: dict = {}


def _predictor(group_id: str):
    """Predictor ép nhóm `group_id`; nạp model 1 lần cho mỗi nhóm."""
    if group_id not in _PREDICTORS:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from captcha.config import Config, default_config_path
        from captcha.predict import Predictor
        from captcha.utils import get_device
        cfg = Config.load(default_config_path(ROOT))
        cfg.by_id(group_id)                   # nhóm không tồn tại → lỗi ngay, không đoán
        _PREDICTORS[group_id] = Predictor(cfg, ROOT, backend="torch",
                                          device=get_device("cpu"), enforce_group=group_id)
    return _PREDICTORS[group_id]


def _flatten_png(img_bytes: bytes) -> bytes:
    """Ảnh có nền trong suốt → dán lên nền trắng (model train trên nền đặc)."""
    from PIL import Image
    im = Image.open(io.BytesIO(img_bytes))
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def solve_captcha(img_bytes: bytes, group_id: str, min_conf: float = 0.5) -> str:
    """
    Giải captcha bằng model của nhóm `group_id`. Model không chắc → PdfFetchError("captcha")
    để hộp chờ thử lại lượt sau (lấy captcha mới), thay vì gửi bừa.
    """
    try:
        png = _flatten_png(img_bytes)
    except Exception as e:  # noqa: BLE001
        raise PdfFetchError(f"Ảnh captcha không đọc được: {e}", "captcha") from e
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        tmp = Path(f.name)
    try:
        pred = _predictor(group_id).predict_path(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if pred.error or not pred.text or pred.confidence < min_conf:
        raise PdfFetchError(
            f"Model không chắc captcha ({group_id}: {pred.text!r}, conf={pred.confidence:.2f})",
            "captcha")
    log.debug("captcha %s → %r (%.2f)", group_id, pred.text, pred.confidence)
    return pred.text
