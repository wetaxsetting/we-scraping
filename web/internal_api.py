"""API nội bộ của engine cho WeInvoice backend (we-scraping-api) gọi.

    POST /internal/gdt/login-test   {"mst": "...", "username": "...", "password": "..."}
      → 200 {"ok": true,  "reason": "ok",          "message": "",  "attempts": n}
      → 200 {"ok": false, "reason": "rejected",    "message": "<câu GDT trả>", ...}   sai tài khoản/mật khẩu
      → 200 {"ok": false, "reason": "unavailable", "message": "...", ...}             hết lượt giải captcha / lỗi mạng
      → 401 thiếu/sai X-Api-Key · 503 chưa đặt SCRAPING_INTERNAL_API_KEY · 400 thiếu trường
    GET  /internal/health → {"ok": true}

Chỉ đăng nhập thử, KHÔNG lưu gì: backend lưu MST + mật khẩu (mã hoá) khi ok=true.
Không bao giờ log hay trả mật khẩu / token GDT.

Chạy:  python -X utf8 web/internal_api.py
Cấu hình (.env): SCRAPING_INTERNAL_API_KEY (bắt buộc), SCRAPING_INTERNAL_HOST (mặc định 127.0.0.1),
SCRAPING_INTERNAL_PORT (mặc định 8090). Chỉ mở trong mạng nội bộ: mật khẩu Cục Thuế đi qua đây.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv không bắt buộc
    pass

import api_client  # noqa: E402

log = logging.getLogger("internal_api")

MAX_RETRIES = 6
# Model captcha + phiên HTTP GDT trong api_client là dùng chung → mỗi lúc 1 lượt đăng nhập.
_LOGIN_LOCK = threading.Lock()
_RETRY_EXHAUSTED = "lần không được"

app = Flask(__name__)


def _authorized() -> tuple[bool, int]:
    expected = os.getenv("SCRAPING_INTERNAL_API_KEY", "").strip()
    if not expected:
        return False, 503
    given = request.headers.get("X-Api-Key", "")
    return hmac.compare_digest(given.encode(), expected.encode()), 401


def classify(result: dict) -> str:
    """ok / rejected (GDT từ chối tài khoản) / unavailable (captcha hết lượt, mạng, GDT lỗi)."""
    if result.get("ok"):
        return "ok"
    msg = result.get("message") or ""
    if _RETRY_EXHAUSTED in msg or int(result.get("attempts") or 0) >= MAX_RETRIES:
        return "unavailable"
    return "rejected"


@app.get("/internal/health")
def health():
    return jsonify({"ok": True})


@app.post("/internal/gdt/login-test")
def login_test():
    ok, status = _authorized()
    if not ok:
        return jsonify({"ok": False, "reason": "unauthorized"}), status
    body = request.get_json(silent=True) or {}
    username = str(body.get("username") or body.get("mst") or "").strip()
    password = str(body.get("password") or "")
    mst = str(body.get("mst") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "reason": "bad_request", "message": "Thiếu username / password"}), 400

    with _LOGIN_LOCK:
        try:
            result = api_client.authenticate_auto(username, password, max_retries=MAX_RETRIES)
        except Exception as exc:  # noqa: BLE001 — mọi lỗi bất ngờ → unavailable, không lộ chi tiết
            log.exception("login-test mst=%s: lỗi bất ngờ", mst or username)
            return jsonify({"ok": False, "reason": "unavailable",
                            "message": f"{type(exc).__name__}", "attempts": 0})
    reason = classify(result)
    log.info("login-test mst=%s user=%s → %s (attempts=%s)", mst, username, reason, result.get("attempts"))
    return jsonify({"ok": reason == "ok", "reason": reason,
                    "message": "" if reason == "ok" else (result.get("message") or ""),
                    "attempts": result.get("attempts")})


def _setup_logging() -> None:
    """Console (nếu có) + file `<INVOICE_OUTPUT_DIR>/logs/internal_api_<yyyy-mm>.log`.

    Task Scheduler chạy bằng pythonw: không có console, nên không có file log thì mất hết dấu vết.
    """
    from datetime import date
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    out = Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output")
    log_dir = out / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_dir / f"internal_api_{date.today():%Y-%m}.log", encoding="utf-8")]
    if sys.stderr is not None:          # pythonw không có console
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = handlers


def main() -> None:
    _setup_logging()
    if not os.getenv("SCRAPING_INTERNAL_API_KEY", "").strip():
        log.warning("Chưa đặt SCRAPING_INTERNAL_API_KEY: mọi request sẽ bị từ chối (503)")
    host = os.getenv("SCRAPING_INTERNAL_HOST", "127.0.0.1")
    port = int(os.getenv("SCRAPING_INTERNAL_PORT", "8090"))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
