"""Cầu nối engine ↔ bảng của WeInvoice (schema `weinvoice`, cùng PostgreSQL với engine).

WeInvoice (we-scraping-api) lưu MST của người dùng + mật khẩu Cục Thuế đã mã hoá ở
`weinvoice.winv_cmpn_infm`. Engine:
  - đưa các MST đã CONNECTED vào bảng `companies` của mình để job tự lấy hoá đơn;
  - giải mã mật khẩu khi đăng nhập GDT. Mật khẩu WeInvoice xét TRƯỚC `HDT_PASSWORD_<MST>` trong `.env`
    (xem `invoice_job.resolve_password`).
  - đọc / cập nhật hộp mail người dùng kết nối (`winv_mlbx_infm`) cho `web/weinvoice_worker.py`
    (quét mail theo MST của hộp; refresh token Microsoft mới được mã hoá lại bằng `encrypt`).

Định dạng mã hoá (khớp `WeInvoiceSecretCipher` bên Java):
    "v1:" + base64(iv[12] || AES-256-GCM(ciphertext || tag[16]))
khoá = base64 của 32 byte, cùng giá trị `WECMS_WEINVOICE_SECRET_KEY` của backend,
đặt trong `.env` engine là `WEINVOICE_SECRET_KEY`.

Không có schema `weinvoice` (máy chưa dựng WeInvoice) → mọi hàm im lặng trả rỗng.
"""

from __future__ import annotations

import base64
import logging
import os
import re

log = logging.getLogger(__name__)

PREFIX = "v1:"
IV_BYTES = 12
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def app_schema() -> str:
    schema = os.getenv("WEINVOICE_APP_SCHEMA", "weinvoice").strip() or "weinvoice"
    if not _SCHEMA_RE.match(schema):
        raise ValueError("WEINVOICE_APP_SCHEMA không hợp lệ")
    return schema


def _key() -> bytes | None:
    raw = os.getenv("WEINVOICE_SECRET_KEY", "").strip()
    if not raw:
        return None
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise ValueError("WEINVOICE_SECRET_KEY phải là base64 của đúng 32 byte")
    return key


def decrypt(value: str | None, key: bytes | None = None) -> str | None:
    """Giải mã 1 giá trị "v1:…". Thiếu khoá / giá trị rỗng → None. Sai khoá → ValueError."""
    if not value:
        return None
    key = key if key is not None else _key()
    if key is None:
        return None
    if not value.startswith(PREFIX):
        raise ValueError("mật khẩu WeInvoice không đúng định dạng v1")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    data = base64.b64decode(value[len(PREFIX):])
    try:
        return AESGCM(key).decrypt(data[:IV_BYTES], data[IV_BYTES:], None).decode("utf-8")
    except InvalidTag as exc:
        raise ValueError("không giải mã được mật khẩu WeInvoice (sai WEINVOICE_SECRET_KEY?)") from exc


def _table_exists(cur, schema: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.winv_cmpn_infm",))
    return bool(cur.fetchone()["ok"])


def register_connected_companies(store) -> int:
    """Thêm vào `companies` các MST đã CONNECTED bên WeInvoice mà engine chưa có.

    Không ghi đè công ty đã có (tài khoản/tên do người vận hành engine đặt giữ nguyên).
    Trả số công ty mới.
    """
    schema = app_schema()
    with store._tx() as cur:
        if not _table_exists(cur, schema):
            return 0
        cur.execute(f"""
            INSERT INTO companies (company_mst, gdt_username, name)
            SELECT DISTINCT ON (c.cmpn_mst) c.cmpn_mst, COALESCE(c.gdt_user_id, c.cmpn_mst), c.cmpn_nm
              FROM {schema}.winv_cmpn_infm c
             WHERE c.conn_stts = 'CONNECTED' AND c.del_yn = 'N' AND c.gdt_pw_enc IS NOT NULL
             ORDER BY c.cmpn_mst, COALESCE(c.mod_dtm, c.reg_dtm) DESC
            ON CONFLICT DO NOTHING
        """)
        added = cur.rowcount
        # MST trước đó chỉ có HĐ tải lên (dòng tạm `#upload:<MST>`, không hoạt động, do
        # web/weinvoice_worker.py tạo) nay đã CONNECTED → dùng tài khoản thật, bật cho job.
        cur.execute(f"""
            UPDATE companies co
               SET gdt_username = src.u, name = COALESCE(co.name, src.n), active = true
              FROM (SELECT DISTINCT ON (c.cmpn_mst) c.cmpn_mst AS mst,
                           COALESCE(c.gdt_user_id, c.cmpn_mst) AS u, c.cmpn_nm AS n
                      FROM {schema}.winv_cmpn_infm c
                     WHERE c.conn_stts = 'CONNECTED' AND c.del_yn = 'N' AND c.gdt_pw_enc IS NOT NULL
                     ORDER BY c.cmpn_mst, COALESCE(c.mod_dtm, c.reg_dtm) DESC) src
             WHERE co.company_mst = src.mst AND co.gdt_username LIKE '#upload:%%'
               AND NOT EXISTS (SELECT 1 FROM companies o WHERE o.gdt_username = src.u)
        """)
        added += cur.rowcount
    if added:
        log.info("WeInvoice: thêm %d MST đã kết nối vào companies", added)
    return added


def mark_login_failed(store, mst: str, gdt_username: str | None, message: str) -> int:
    """Thuế từ chối mật khẩu WeInvoice của MST này → `conn_stts = 'ERROR'` + câu của Thuế.

    Màn "Kết nối Cục Thuế" hiện lỗi để người dùng cập nhật mật khẩu; engine thôi dùng mật khẩu
    đó (`register_connected_companies` / `password_for` chỉ đọc dòng CONNECTED).
    """
    from datetime import datetime, timezone
    schema = app_schema()
    with store._tx() as cur:
        if not _table_exists(cur, schema):
            return 0
        dtm = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        cur.execute(f"""
            UPDATE {schema}.winv_cmpn_infm
               SET conn_stts = 'ERROR', conn_msg = %s, conn_chk_dtm = %s, mod_id = 'engine', mod_dtm = %s
             WHERE cmpn_mst = %s AND COALESCE(gdt_user_id, cmpn_mst) = %s
               AND conn_stts = 'CONNECTED' AND del_yn = 'N'
        """, ((message or "Cục Thuế từ chối mật khẩu")[:500], dtm, dtm, mst, gdt_username or mst))
        return cur.rowcount


def password_for(store, mst: str, gdt_username: str | None = None) -> str | None:
    """Mật khẩu GDT mới nhất của `mst` trong WeInvoice (ưu tiên đúng tài khoản `gdt_username`)."""
    schema = app_schema()
    with store._tx() as cur:
        if not _table_exists(cur, schema):
            return None
        cur.execute(f"""
            SELECT c.gdt_pw_enc
              FROM {schema}.winv_cmpn_infm c
             WHERE c.cmpn_mst = %s AND c.conn_stts = 'CONNECTED' AND c.del_yn = 'N'
               AND c.gdt_pw_enc IS NOT NULL
             ORDER BY (COALESCE(c.gdt_user_id, c.cmpn_mst) = %s) DESC,
                      COALESCE(c.mod_dtm, c.reg_dtm) DESC
             LIMIT 1
        """, (mst, gdt_username or mst))
        row = cur.fetchone()
    return decrypt(row["gdt_pw_enc"]) if row else None


def encrypt(plain: str, key: bytes | None = None, iv: bytes | None = None) -> str:
    """Mã hoá như `WeInvoiceSecretCipher.encrypt` (Java): "v1:" + base64(iv[12] || ct || tag[16]).

    Thiếu khoá → ValueError (không bao giờ lưu bí mật dạng rõ). `iv` chỉ để test; mặc định ngẫu nhiên.
    """
    key = key if key is not None else _key()
    if key is None:
        raise ValueError("Chưa đặt WEINVOICE_SECRET_KEY: không mã hoá được")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = iv if iv is not None else os.urandom(IV_BYTES)
    if len(iv) != IV_BYTES:
        raise ValueError("iv phải dài 12 byte")
    return PREFIX + base64.b64encode(iv + AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)).decode("ascii")


# ---------------------------------------------------------------------------
# Hộp mail người dùng kết nối (weinvoice.winv_mlbx_infm)
# ---------------------------------------------------------------------------

def ms_client() -> tuple[str, str] | None:
    """(client_id, client_secret) app Microsoft của WeInvoice (`WEINVOICE_MS_CLIENT_ID` / `_SECRET`,
    cùng giá trị `WECMS_WEINVOICE_MS_CLIENT_ID` / `_SECRET` của backend). Thiếu → None."""
    cid = os.getenv("WEINVOICE_MS_CLIENT_ID", "").strip()
    secret = os.getenv("WEINVOICE_MS_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


MAILBOX_TABLE = "winv_mlbx_infm"
_MAILBOX_COLS = ("mlbx_seq, owner_id, mlbx_kind, mail_addr, auth_type, imap_host, imap_port, tls_mode, "
                 "login_id, secret_enc, fldr_list, cmpn_scope, cmpn_mst, conn_stts, conn_msg, "
                 "last_scan_dtm, last_scan_cnt")


def _utc_dtm(offset_seconds: float = 0) -> str:
    """yyyyMMddHHmmss giờ UTC — quy ước các cột *_dtm của WeInvoice (`WeInvoiceTime.nowDtm` bên Java)."""
    from datetime import datetime, timezone
    import time
    return datetime.fromtimestamp(time.time() + offset_seconds, timezone.utc).strftime("%Y%m%d%H%M%S")


def _mailbox_table_exists(cur, schema: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{MAILBOX_TABLE}",))
    return bool(cur.fetchone()["ok"])


def list_mailboxes(store, status: str | None = "CONNECTED", scanned_before: str | None = None) -> list[dict]:
    """Hộp mail chưa xoá (mặc định chỉ CONNECTED). `scanned_before` (dtm UTC): chỉ hộp chưa quét từ mốc đó.
    Bảng chưa có (chưa chạy V2) → []."""
    schema = app_schema()
    with store._tx() as cur:
        if not _mailbox_table_exists(cur, schema):
            return []
        cur.execute(f"""
            SELECT {_MAILBOX_COLS} FROM {schema}.{MAILBOX_TABLE}
             WHERE del_yn = 'N'
               AND (%(st)s::text IS NULL OR conn_stts = %(st)s)
               AND (%(before)s::text IS NULL OR last_scan_dtm IS NULL OR last_scan_dtm < %(before)s)
             ORDER BY COALESCE(last_scan_dtm, ''), mlbx_seq
        """, {"st": status, "before": scanned_before})
        return [dict(r) for r in cur.fetchall()]


def user_mailbox_accounts(store) -> list[str]:
    """Mọi địa chỉ hộp mail người dùng WeInvoice (kể cả đã xoá), chữ thường. Luồng mail `.env` (Webcash)
    bỏ qua file / link của các tài khoản này: chúng chỉ được khớp vào MST của chính hộp đó. Bảng chưa có → []."""
    schema = app_schema()
    with store._tx() as cur:
        if not _mailbox_table_exists(cur, schema):
            return []
        cur.execute(f"SELECT DISTINCT lower(mail_addr) AS a FROM {schema}.{MAILBOX_TABLE}")
        return [r["a"] for r in cur.fetchall() if r["a"]]


def get_mailbox(store, mailbox_id: int) -> dict | None:
    """1 hộp mail chưa xoá (mọi trạng thái), hoặc None."""
    schema = app_schema()
    with store._tx() as cur:
        if not _mailbox_table_exists(cur, schema):
            return None
        cur.execute(f"SELECT {_MAILBOX_COLS} FROM {schema}.{MAILBOX_TABLE} WHERE mlbx_seq = %s AND del_yn = 'N'",
                    (int(mailbox_id),))
        row = cur.fetchone()
    return dict(row) if row else None


def mailbox_msts(store, mailbox: dict) -> set[str]:
    """MST mà hộp mail này được gắn HĐ vào: ONE → cmpn_mst; ALL → mọi MST chưa xoá của chủ hộp."""
    if (mailbox.get("cmpn_scope") or "ONE").upper() != "ALL":
        mst = (mailbox.get("cmpn_mst") or "").strip()
        return {mst} if mst else set()
    schema = app_schema()
    with store._tx() as cur:
        if not _table_exists(cur, schema):
            return set()
        cur.execute(f"SELECT DISTINCT cmpn_mst FROM {schema}.winv_cmpn_infm WHERE owner_id = %s AND del_yn = 'N'",
                    (mailbox["owner_id"],))
        return {r["cmpn_mst"] for r in cur.fetchall() if r["cmpn_mst"]}


def claim_mailbox_scan(store, mailbox_id: int, scanned_before: str) -> bool:
    """Giữ chỗ quét định kỳ: đặt last_scan_dtm = bây giờ nếu hộp chưa quét từ `scanned_before`.

    False = vừa có lượt khác quét (worker khác / người dùng bấm "Quét ngay") → bỏ qua.
    """
    schema = app_schema()
    dtm = _utc_dtm()
    with store._tx() as cur:
        cur.execute(f"""
            UPDATE {schema}.{MAILBOX_TABLE} SET last_scan_dtm = %s
             WHERE mlbx_seq = %s AND del_yn = 'N' AND conn_stts = 'CONNECTED'
               AND (last_scan_dtm IS NULL OR last_scan_dtm < %s)
        """, (dtm, int(mailbox_id), scanned_before))
        return cur.rowcount == 1


def update_mailbox_scan(store, mailbox_id: int, status: str | None, message: str | None,
                        scan_cnt: int | None = None, scanned: bool = False,
                        secret_enc: str | None = None) -> int:
    """Ghi kết quả 1 lượt quét. status None = giữ nguyên conn_stts (vd lỗi mạng tạm thời).

    scanned=True → last_scan_dtm = bây giờ + last_scan_cnt. secret_enc (đã mã hoá) → thay bí mật
    (Microsoft cấp refresh token mới). Không bao giờ nhận bí mật dạng rõ.
    """
    if secret_enc is not None and not secret_enc.startswith(PREFIX):
        raise ValueError("secret_enc phải là giá trị đã mã hoá v1")
    schema = app_schema()
    dtm = _utc_dtm()
    with store._tx() as cur:
        cur.execute(f"""
            UPDATE {schema}.{MAILBOX_TABLE}
               SET conn_stts = COALESCE(%(st)s, conn_stts),
                   conn_msg = %(msg)s,
                   last_scan_dtm = CASE WHEN %(scanned)s THEN %(dtm)s ELSE last_scan_dtm END,
                   last_scan_cnt = CASE WHEN %(scanned)s THEN %(cnt)s ELSE last_scan_cnt END,
                   secret_enc = COALESCE(%(sec)s, secret_enc),
                   mod_id = 'engine', mod_dtm = %(dtm)s
             WHERE mlbx_seq = %(id)s AND del_yn = 'N'
        """, {"st": status, "msg": (message or None) and message[:500], "scanned": bool(scanned),
              "dtm": dtm, "cnt": scan_cnt, "sec": secret_enc, "id": int(mailbox_id)})
        return cur.rowcount
