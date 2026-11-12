"""Hộp mail WeInvoice trên PostgreSQL thật: đọc / giữ chỗ / ghi kết quả quét (`weinvoice_bridge`) và
lọc file / link theo tài khoản mail (`InvoiceStore`). Schema tạm, xóa khi xong.

Không đặt TEST_DATABASE_URL → toàn bộ file được bỏ qua (như tests/test_invoice_store.py).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="đặt TEST_DATABASE_URL để chạy test DB")

import weinvoice_bridge  # noqa: E402
from invoice_store import InvoiceStore  # noqa: E402

KEY = bytes(range(32))


@pytest.fixture
def store(monkeypatch):
    schema = f"test_winv_mail_{uuid.uuid4().hex[:10]}"
    s = InvoiceStore(DSN, schema=schema)
    s.init_schema()
    monkeypatch.setenv("WEINVOICE_APP_SCHEMA", schema)
    with s._tx() as cur:
        cur.execute(f"""
            CREATE TABLE {schema}.winv_cmpn_infm (
                cmpn_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, owner_id varchar(100) NOT NULL,
                cmpn_mst varchar(14) NOT NULL, cmpn_nm varchar(255), gdt_user_id varchar(100), gdt_pw_enc text,
                conn_stts varchar(20) NOT NULL DEFAULT 'CONNECTED', conn_msg varchar(500), conn_chk_dtm varchar(14),
                del_yn char(1) NOT NULL DEFAULT 'N', reg_dtm varchar(14), mod_id varchar(100), mod_dtm varchar(14));
            CREATE TABLE {schema}.winv_mlbx_infm (
                mlbx_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, owner_id varchar(100) NOT NULL,
                mlbx_kind varchar(10) NOT NULL, mail_addr varchar(320) NOT NULL, auth_type varchar(20) NOT NULL,
                imap_host varchar(255), imap_port int, tls_mode varchar(10), login_id varchar(320) NOT NULL,
                secret_enc text NOT NULL, fldr_list varchar(500) NOT NULL DEFAULT 'INBOX',
                cmpn_scope varchar(5) NOT NULL DEFAULT 'ONE', cmpn_mst varchar(14),
                conn_stts varchar(20) NOT NULL DEFAULT 'CONNECTED', conn_msg varchar(500),
                last_scan_dtm varchar(14), last_scan_cnt int, del_yn char(1) NOT NULL DEFAULT 'N',
                reg_id varchar(100), reg_dtm varchar(14), mod_id varchar(100), mod_dtm varchar(14));
            INSERT INTO {schema}.winv_cmpn_infm (owner_id, cmpn_mst, del_yn) VALUES
                ('u1', '0100000001', 'N'), ('u1', '0100000002', 'N'), ('u1', '0100000003', 'Y'), ('u2', '0200000001', 'N');
        """)
    try:
        yield s
    finally:
        s.conn.rollback()
        with s.conn, s.conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        s.close()


def _add_mailbox(store, **kw):
    row = {"owner_id": "u1", "mlbx_kind": "GMAIL", "mail_addr": "a@gmail.com", "auth_type": "APP_PASSWORD",
           "login_id": "a@gmail.com", "secret_enc": weinvoice_bridge.encrypt("pw", KEY),
           "cmpn_scope": "ONE", "cmpn_mst": "0100000001", **kw}
    cols = ", ".join(row)
    with store._tx() as cur:
        cur.execute(f"INSERT INTO {weinvoice_bridge.app_schema()}.winv_mlbx_infm ({cols}) "
                    f"VALUES ({', '.join(['%s'] * len(row))}) RETURNING mlbx_seq", list(row.values()))
        return cur.fetchone()["mlbx_seq"]


def test_mailbox_list_scope_claim_and_update(store):
    a = _add_mailbox(store)
    b = _add_mailbox(store, mail_addr="b@x.vn", cmpn_scope="ALL", cmpn_mst=None, last_scan_dtm="20991231000000")
    _add_mailbox(store, mail_addr="c@x.vn", conn_stts="ERROR")
    _add_mailbox(store, mail_addr="d@x.vn", del_yn="Y")
    assert [m["mlbx_seq"] for m in weinvoice_bridge.list_mailboxes(store)] == [a, b]
    due = weinvoice_bridge.list_mailboxes(store, "CONNECTED", scanned_before="20260101000000")
    assert [m["mlbx_seq"] for m in due] == [a]
    assert weinvoice_bridge.mailbox_msts(store, weinvoice_bridge.get_mailbox(store, a)) == {"0100000001"}
    assert weinvoice_bridge.mailbox_msts(store, weinvoice_bridge.get_mailbox(store, b)) == {"0100000001", "0100000002"}

    before = weinvoice_bridge._utc_dtm(-60)            # "chưa quét từ 1 phút trước"
    assert weinvoice_bridge.claim_mailbox_scan(store, a, before) is True
    assert weinvoice_bridge.claim_mailbox_scan(store, a, before) is False          # đã có lượt giữ chỗ

    enc = weinvoice_bridge.encrypt("RT-NEW", KEY)
    weinvoice_bridge.update_mailbox_scan(store, a, "EXPIRED", "x" * 900, None, secret_enc=enc)
    m = weinvoice_bridge.get_mailbox(store, a)
    assert m["conn_stts"] == "EXPIRED" and len(m["conn_msg"]) == 500 and m["secret_enc"] == enc
    weinvoice_bridge.update_mailbox_scan(store, a, None, "mạng lỗi")
    assert weinvoice_bridge.get_mailbox(store, a)["conn_stts"] == "EXPIRED"     # None = giữ trạng thái
    weinvoice_bridge.update_mailbox_scan(store, a, "CONNECTED", None, 3, scanned=True)
    m = weinvoice_bridge.get_mailbox(store, a)
    assert m["conn_stts"] == "CONNECTED" and m["conn_msg"] is None and m["last_scan_cnt"] == 3
    assert len(m["last_scan_dtm"]) == 14 and m["secret_enc"] == enc


def test_mailbox_table_missing_means_no_mailboxes(store, monkeypatch):
    monkeypatch.setenv("WEINVOICE_APP_SCHEMA", "khong_co_schema_nay")
    assert weinvoice_bridge.list_mailboxes(store) == []
    assert weinvoice_bridge.get_mailbox(store, 1) is None


def test_store_filters_files_and_links_by_mail_account(store):
    e1 = store.record_email("user@y.vn/INBOX", "<1@x>", True, "unmatched")
    e2 = store.record_email("webcash@x.vn/HDDT", "<2@x>", True, "unmatched")
    for eid in (e1, e2):
        store.add_email_file(eid, {"filename": "a.xml", "kind": "xml", "sha256": f"s{eid}",
                                   "saved_path": "mail/unmatched/a.xml", "status": "unmatched"})
        store.add_email_links(eid, [{"url": f"https://tracuu.x.vn/?k={eid}", "host": "tracuu.x.vn"}])
    assert {f["email_id"] for f in store.unmatched_email_files()} == {e1, e2}
    assert [f["email_id"] for f in store.unmatched_email_files(mailboxes=["user@y.vn"])] == [e1]
    assert {l["email_id"] for l in store.pending_email_links()} == {e1, e2}
    assert [l["email_id"] for l in store.pending_email_links(mailboxes=["webcash@x.vn"])] == [e2]
