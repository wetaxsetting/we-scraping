"""Cầu nối engine ↔ bảng của WeInvoice (schema `weinvoice`, cùng PostgreSQL với engine).

WeInvoice (we-scraping-api) lưu MST của người dùng + mật khẩu Cục Thuế đã mã hoá ở
`weinvoice.winv_cmpn_infm`. Engine:
  - đưa các MST đã CONNECTED vào bảng `companies` của mình để job tự lấy hoá đơn;
  - giải mã mật khẩu khi đăng nhập GDT cho MST không có `HDT_PASSWORD_<MST>` trong `.env`.

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
    if added:
        log.info("WeInvoice: thêm %d MST đã kết nối vào companies", added)
    return added


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
