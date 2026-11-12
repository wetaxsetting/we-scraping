"""API nội bộ của engine cho WeInvoice backend (we-scraping-api) gọi.

    POST /internal/gdt/login-test   {"mst": "...", "username": "...", "password": "..."}
      → 200 {"ok": true,  "reason": "ok",          "message": "",  "attempts": n}
      → 200 {"ok": false, "reason": "rejected",    "message": "<câu GDT trả>", ...}   sai tài khoản/mật khẩu
      → 200 {"ok": false, "reason": "unavailable", "message": "...", ...}             hết lượt giải captcha / lỗi mạng
      → 401 thiếu/sai X-Api-Key · 503 chưa đặt SCRAPING_INTERNAL_API_KEY · 400 thiếu trường
    POST /internal/mail/login-test  {"auth": "PASSWORD|APP_PASSWORD|OAUTH_MS", "host", "port", "tls_mode": "SSL|STARTTLS",
                                     "login_id", "password"?, "refresh_token"?, "folders": [...]}
      → 200 {"ok": bool, "message": "...", "missing_folders": [...], "reason": "..."}   (xem mail_login_test)
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
import gdt_login_guard  # noqa: E402
import weinvoice_bridge  # noqa: E402

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
    if result.get("reason") == "captcha":
        return "unavailable"
    if result.get("reason") in ("bad_credentials", "locked"):
        return "rejected"
    msg = result.get("message") or ""
    if _RETRY_EXHAUSTED in msg or int(result.get("attempts") or 0) >= MAX_RETRIES:
        return "unavailable"
    return "rejected"


_STORE = None


def _guard_store():
    """Kết nối DB cho chặn đăng nhập (gdt_login_guard). Không có DATABASE_URL / DB lỗi → None:
    vẫn cho người dùng thử (họ tự gõ mật khẩu), chỉ mất phần nhớ mật khẩu sai."""
    global _STORE
    if _STORE is None and os.getenv("DATABASE_URL"):
        try:
            from invoice_store import InvoiceStore
            _STORE = InvoiceStore(os.environ["DATABASE_URL"])
        except Exception:  # noqa: BLE001
            log.exception("Không kết nối được DB cho chặn đăng nhập")
    return _STORE


def _guard(fn):
    global _STORE
    try:
        return fn()
    except Exception:  # noqa: BLE001 — DB rớt: bỏ kết nối, lần sau mở lại
        log.exception("gdt_login_guard lỗi")
        _STORE = None
        return None


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
        store = _guard_store()
        # Đúng mật khẩu này Thuế đã từ chối → không gửi lại (Thuế khóa tài khoản sau 5 lần sai)
        blocked = _guard(lambda: gdt_login_guard.check(store, username, password)) if store else None
        if blocked:
            log.info("login-test mst=%s user=%s → chặn (mật khẩu đã bị Thuế từ chối)", mst, username)
            return jsonify({"ok": False, "reason": "rejected", "attempts": 0,
                            "message": ("Mật khẩu này đã bị Cục Thuế từ chối"
                                        + (" (tài khoản đang bị khóa)" if blocked["reason"] == "locked" else "")
                                        + f": {blocked.get('message') or ''}. Không gửi lại để tránh khóa tài khoản.")})
        try:
            result = api_client.authenticate_auto(username, password, max_retries=MAX_RETRIES)
        except Exception as exc:  # noqa: BLE001 — mọi lỗi bất ngờ → unavailable, không lộ chi tiết
            log.exception("login-test mst=%s: lỗi bất ngờ", mst or username)
            return jsonify({"ok": False, "reason": "unavailable",
                            "message": f"{type(exc).__name__}", "attempts": 0})
        if store and result.get("ok"):
            _guard(lambda: gdt_login_guard.clear(store, username))
        elif store and result.get("reason") in gdt_login_guard.REASONS:
            _guard(lambda: gdt_login_guard.record(store, username, mst or None, result["reason"],
                                                  result.get("message") or "", password))
    reason = classify(result)
    log.info("login-test mst=%s user=%s → %s (attempts=%s)", mst, username, reason, result.get("attempts"))
    return jsonify({"ok": reason == "ok", "reason": reason,
                    "message": "" if reason == "ok" else (result.get("message") or ""),
                    "attempts": result.get("attempts")})


# ---------------------------------------------------------------------------
# Hộp mail người dùng kết nối (màn "Hộp mail" của WeInvoice)
# ---------------------------------------------------------------------------

MAIL_TIMEOUT = 30           # giây, cho kết nối IMAP (lấy token Microsoft cũng 30 s)
MAIL_AUTHS = ("PASSWORD", "APP_PASSWORD", "OAUTH_MS")


def _mail_result(ok: bool, message: str, missing: list | None = None, reason: str | None = None):
    return jsonify({"ok": ok, "message": message, "missing_folders": missing or [],
                    "reason": reason or ("ok" if ok else "rejected")})


@app.post("/internal/mail/login-test")
def mail_login_test():
    """Đăng nhập thử hộp mail + EXAMINE từng thư mục (không đọc mail nào, không lưu gì).

    ok=true khi đăng nhập được VÀ mở được ít nhất 1 thư mục yêu cầu; thư mục không có → missing_folders
    (mặc định thư mục là INBOX). Không mở được thư mục nào → ok=false (quét sẽ không có gì để đọc).
    reason (thêm ngoài hợp đồng): ok | rejected (máy chủ từ chối) | expired (Microsoft invalid_grant)
    | unavailable (mạng / máy chủ lỗi) | config (engine chưa cấu hình Microsoft).
    """
    import mail_reader
    ok, status = _authorized()
    if not ok:
        return jsonify({"ok": False, "reason": "unauthorized"}), status
    body = request.get_json(silent=True) or {}
    auth = str(body.get("auth") or "").strip().upper()
    host = str(body.get("host") or "").strip()
    login_id = str(body.get("login_id") or "").strip()
    tls_mode = str(body.get("tls_mode") or "SSL").strip().upper()
    folders = body.get("folders") or ["INBOX"]
    if isinstance(folders, str):
        folders = folders.split(",")
    folders = [str(f).strip() for f in folders if str(f).strip()] or ["INBOX"]
    try:
        port = int(body.get("port") or 993)
    except (TypeError, ValueError):
        port = 0
    bad = []
    if auth not in MAIL_AUTHS:
        bad.append("auth")
    if not host:
        bad.append("host")
    if not login_id:
        bad.append("login_id")
    if not 0 < port < 65536:
        bad.append("port")
    if tls_mode not in mail_reader.TLS_MODES:
        bad.append("tls_mode")
    secret = str(body.get("refresh_token" if auth == "OAUTH_MS" else "password") or "")
    if auth in MAIL_AUTHS and not secret:
        bad.append("refresh_token" if auth == "OAUTH_MS" else "password")
    if bad:
        return jsonify({"ok": False, "reason": "bad_request", "missing_folders": [],
                        "message": "Thiếu / sai trường: " + ", ".join(bad)}), 400

    if auth == "OAUTH_MS":
        client = weinvoice_bridge.ms_client()
        if client is None:
            return _mail_result(False, "Engine chưa cấu hình đăng nhập Microsoft "
                                       "(WEINVOICE_MS_CLIENT_ID / WEINVOICE_MS_CLIENT_SECRET)", reason="config")
        kw = {"auth": "oauth2_microsoft",
              "token_provider": lambda: mail_reader.microsoft_refresh_token(client[0], client[1], secret)[0]}
    else:
        kw = {"auth": "password", "password": secret}

    try:
        opened, missing = mail_reader.check_login(host, port, login_id, folders, tls_mode=tls_mode,
                                                  timeout=MAIL_TIMEOUT, **kw)
    except mail_reader.MailAuthExpired as e:
        log.info("mail login-test %s@%s:%s → expired", login_id, host, port)
        return _mail_result(False, f"Phiên đăng nhập Microsoft đã hết hạn hoặc bị thu hồi, hãy đăng nhập lại ({e})",
                            reason="expired")
    except mail_reader.MailLoginRefused as e:
        log.info("mail login-test %s@%s:%s → rejected", login_id, host, port)
        hint = (" Gmail cần Mật khẩu ứng dụng (16 ký tự) và đã bật IMAP."
                if host.lower() == "imap.gmail.com" else "")
        return _mail_result(False, str(e) + hint, reason="rejected")
    except mail_reader.MailError as e:
        log.info("mail login-test %s@%s:%s → unavailable: %s", login_id, host, port, e)
        return _mail_result(False, str(e), reason="unavailable")
    except Exception as exc:  # noqa: BLE001 — lỗi bất ngờ: không lộ chi tiết (có thể dính bí mật)
        log.exception("mail login-test %s@%s:%s: lỗi bất ngờ", login_id, host, port)
        return _mail_result(False, f"Lỗi engine khi đăng nhập thử: {type(exc).__name__}", reason="unavailable")
    log.info("mail login-test %s@%s:%s → opened=%s missing=%s", login_id, host, port, opened, missing)
    if not opened:
        return _mail_result(False, "Đăng nhập được nhưng không mở được thư mục nào: " + ", ".join(missing),
                            missing, reason="rejected")
    msg = "" if not missing else "Không có thư mục: " + ", ".join(missing)
    return _mail_result(True, msg, missing)


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
