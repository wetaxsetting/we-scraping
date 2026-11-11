"""Chặn đăng nhập GDT bằng mật khẩu đã bị Thuế từ chối — tránh khóa tài khoản.

Thuế khóa tài khoản sau 5 lần nhập sai (đo 04–05/11/2026: 3600823561, 3603512483 bị khóa sau
5 lượt `tick` / `sync-lists` / `sweep`; 0318793907 sau 5 lượt worker XML cách nhau 15 phút).
Trước đây mỗi lượt chạy lại thử đúng mật khẩu sai đó.

Quy tắc:
  - Thuế báo sai tài khoản/mật khẩu hoặc tài khoản bị khóa → ghi 1 dòng `gdt_login_blocks`
    (tài khoản + dấu vân tay HMAC của mật khẩu đã dùng). Mỗi mật khẩu bị từ chối 1 dòng, nên 2 máy
    giữ 2 mật khẩu khác nhau cũng không thay nhau xóa chặn.
  - Lần sau định đăng nhập: cùng tài khoản + mật khẩu đã bị từ chối → KHÔNG gọi Thuế.
    Mật khẩu mới (sửa `.env` / cập nhật trên WeInvoice) → chưa có trong danh sách → được thử.
  - Đăng nhập được → xóa mọi chặn của tài khoản.
  - Sai captcha KHÔNG chặn (thử lại với ảnh mới).
  - Mở chặn bằng tay (vd Thuế đã mở khóa mà không đổi mật khẩu):
      python -X utf8 web/invoice_job.py unblock-login --username <tài khoản>   (bật lại công ty)

Vân tay = HMAC-SHA256(khóa, tài khoản + mật khẩu). Khóa = `WEINVOICE_SECRET_KEY` (dùng chung
giữa các máy engine), không có thì file `<INVOICE_OUTPUT_DIR>/.gdt_login_key` sinh ngẫu nhiên.
Không lưu mật khẩu, không log mật khẩu.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
REASONS = ("bad_credentials", "locked")

_DDL = """
CREATE TABLE IF NOT EXISTS gdt_login_blocks (
    gdt_username   TEXT NOT NULL,
    company_mst    TEXT,
    reason         TEXT NOT NULL CHECK (reason IN ('bad_credentials', 'locked')),
    message        TEXT,
    pw_fingerprint TEXT NOT NULL,
    hits           INT NOT NULL DEFAULT 0,      -- số lần đã chặn (không gọi Thuế)
    blocked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (gdt_username, pw_fingerprint)
)
"""
_ready: set = set()


def _key() -> bytes:
    raw = os.getenv("WEINVOICE_SECRET_KEY", "").strip()
    if raw:
        try:
            return base64.b64decode(raw)
        except ValueError:
            pass
    path = Path(os.getenv("INVOICE_OUTPUT_DIR") or ROOT / "output") / ".gdt_login_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(32))
    return path.read_bytes()


def fingerprint(username: str, password: str) -> str:
    return hmac.new(_key(), f"{username}\0{password}".encode("utf-8"), hashlib.sha256).hexdigest()


def _ensure(store) -> None:
    if id(store) in _ready:
        return
    with store._tx() as cur:
        cur.execute(_DDL)
    _ready.add(id(store))


def check(store, username: str, password: str) -> dict | None:
    """Chặn còn hiệu lực cho đúng tài khoản + mật khẩu này (đếm thêm 1 lần chặn), hoặc None
    (mật khẩu chưa từng bị Thuế từ chối → được thử)."""
    _ensure(store)
    with store._tx() as cur:
        cur.execute("""UPDATE gdt_login_blocks SET hits = hits + 1, updated_at = now()
                        WHERE gdt_username=%s AND pw_fingerprint=%s RETURNING *""",
                    (username, fingerprint(username, password)))
        return cur.fetchone()


def record(store, username: str, company_mst: str | None, reason: str, message: str, password: str) -> None:
    """Thuế từ chối tài khoản/mật khẩu này → chặn các lần sau dùng đúng mật khẩu đó."""
    if reason not in REASONS:
        return
    _ensure(store)
    with store._tx() as cur:
        cur.execute("""
            INSERT INTO gdt_login_blocks (gdt_username, company_mst, reason, message, pw_fingerprint)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (gdt_username, pw_fingerprint) DO UPDATE SET
                company_mst = EXCLUDED.company_mst, reason = EXCLUDED.reason,
                message = EXCLUDED.message, updated_at = now()
        """, (username, company_mst, reason, (message or "")[:500], fingerprint(username, password)))
    log.error("[%s] Thuế từ chối (%s): %s → DỪNG đăng nhập tài khoản này tới khi cập nhật mật khẩu",
              username, reason, message)


def clear(store, username: str) -> int:
    _ensure(store)
    with store._tx() as cur:
        cur.execute("DELETE FROM gdt_login_blocks WHERE gdt_username=%s", (username,))
        return cur.rowcount


def unblock(store, username: str | None = None, company_mst: str | None = None) -> list:
    """Bỏ chặn bằng tay; bật lại công ty (`active`) đã bị tắt khi tài khoản bị khóa. Trả MST đã bỏ chặn."""
    _ensure(store)
    with store._tx() as cur:
        cur.execute("""DELETE FROM gdt_login_blocks WHERE gdt_username=%s OR company_mst=%s
                       RETURNING gdt_username, company_mst""", (username or "", company_mst or ""))
        rows = cur.fetchall()
        msts = sorted({r["company_mst"] for r in rows if r["company_mst"]} | ({company_mst} if company_mst else set()))
        users = sorted({r["gdt_username"] for r in rows} | ({username} if username else set()))
        cur.execute("""UPDATE companies SET active = true
                        WHERE (company_mst = ANY(%s) OR gdt_username = ANY(%s)) AND NOT active
                          AND gdt_username NOT LIKE '#upload:%%'""", (msts, users))
        return msts or users


def list_blocks(store) -> list:
    _ensure(store)
    with store._tx() as cur:
        cur.execute("SELECT * FROM gdt_login_blocks ORDER BY blocked_at")
        return cur.fetchall()


def vn_time(ts) -> str:
    """Giờ Việt Nam để hiển thị (DB có thể đặt múi giờ khác)."""
    if ts is None:
        return "?"
    from datetime import timedelta, timezone
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone(timedelta(hours=7)))
    return ts.strftime("%d/%m %H:%M")


def describe(row: dict) -> str:
    what = "tài khoản bị Thuế khóa" if row["reason"] == "locked" else "Thuế báo sai tên đăng nhập/mật khẩu"
    when = vn_time(row.get("blocked_at"))
    return (f"Không đăng nhập: {what} lúc {when} ({row.get('message') or ''}). "
            "Cập nhật mật khẩu (trên WeInvoice, hoặc HDT_PASSWORD_<MST> trong .env) để thử lại")
