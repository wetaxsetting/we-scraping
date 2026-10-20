"""Hộp chờ trên PostgreSQL thật: chống trùng, thử lại, cache token, bước XML đầu-cuối.

Cần 1 database PostgreSQL. Test tạo schema tạm riêng (test_invoice_<ngẫu nhiên>) và
xóa khi xong, nên không đụng bảng thật — nhưng vẫn nên trỏ vào DB dev, không phải prod.

Run::

    $env:TEST_DATABASE_URL = "postgresql://user:pass@localhost:5432/dev"
    pytest tests/test_invoice_store.py -q

Không đặt TEST_DATABASE_URL → toàn bộ file được bỏ qua.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="đặt TEST_DATABASE_URL để chạy test DB")

import api_client                     # noqa: E402
import job_policy                     # noqa: E402
from invoice_store import InvoiceStore  # noqa: E402

COMPANY = "1201496252"


def _load_invoice_job():
    spec = importlib.util.spec_from_file_location("invoice_job", ROOT / "web" / "invoice_job.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store():
    schema = f"test_invoice_{uuid.uuid4().hex[:10]}"
    s = InvoiceStore(DSN, schema=schema)
    s.lock_key = int(uuid.uuid4().int % 2**62)   # không tranh khóa với job thật
    s.init_schema()
    s.upsert_company(COMPANY, f"{COMPANY}-U002", "Cty test")
    try:
        yield s
    finally:
        s.conn.rollback()
        with s.conn, s.conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        s.close()


def _row(shdon, nbmst="0100112233", tdlap="2026-10-11T17:00:00Z"):
    raw = {"nbmst": nbmst, "nbten": "NCC A", "khhdon": "C26TAA", "shdon": shdon,
           "khmshdon": 1, "tdlap": tdlap, "tgtcthue": 100, "tgtthue": 8, "tgtttbso": 108}
    return job_policy.normalize_list_row(raw, COMPANY, "mua")


def _count(store, sql, *params):
    with store._tx() as cur:
        cur.execute(sql, params)
        return list(cur.fetchone().values())[0]


# ---------------------------------------------------------------------------

def test_upsert_is_idempotent(store):
    assert store.upsert_invoices([_row(1), _row(2)]) == 2
    assert store.upsert_invoices([_row(1), _row(2), _row(3)]) == 1      # chỉ HĐ 3 là mới
    # cùng số HĐ nhưng khác người bán → HĐ khác
    assert store.upsert_invoices([_row(1, nbmst="0311223344")]) == 1
    assert _count(store, "SELECT count(*) FROM tasks WHERE task_type='xml'") == 4
    assert _count(store, "SELECT ngay_lap FROM invoices WHERE shdon=3") == date(2026, 10, 12)


def test_failed_task_waits_then_dies(store):
    store.upsert_invoices([_row(1)])
    [task] = store.claim_tasks("xml", COMPANY, 10)
    assert store.claim_tasks("xml", COMPANY, 10) == []                  # đang running
    assert store.fail_task(task, "network", "timeout", None) == "pending"
    assert store.claim_tasks("xml", COMPANY, 10) == []                  # chưa đến giờ thử lại

    with store._tx() as cur:                                            # tua thời gian
        cur.execute("UPDATE tasks SET next_run_at = now() - interval '1 minute'")
    [task] = store.claim_tasks("xml", COMPANY, 10)
    assert task["attempts"] == 1
    assert store.fail_task(task, "parse", "XML hỏng", None) == "dead"
    assert store.requeue("dead", task_type="xml") == 1
    assert len(store.claim_tasks("xml", COMPANY, 10)) == 1


def test_release_does_not_count_attempt(store):
    store.upsert_invoices([_row(1)])
    [task] = store.claim_tasks("xml", COMPANY, 10)
    store.release_tasks([task["task_id"]])
    [again] = store.claim_tasks("xml", COMPANY, 10)
    assert again["attempts"] == 0


def test_reclaim_stale(store):
    store.upsert_invoices([_row(1)])
    store.claim_tasks("xml", COMPANY, 10)
    with store._tx() as cur:
        cur.execute("UPDATE tasks SET locked_at = now() - interval '2 hours'")
    assert store.reclaim_stale(60) == 1
    assert len(store.claim_tasks("xml", COMPANY, 10)) == 1


def test_complete_xml_creates_pdf_task_and_partner(store):
    store.upsert_invoices([_row(1)])
    [task] = store.claim_tasks("xml", COMPANY, 10)
    parsed = {"thong_tin_chung": {"mst_tcgp": "0105987432"}, "ma_tra_cuu": "X2KI57TA8",
              "lookup_url": "http://x.easyinvoice.com.vn", "tt_khac": {"Fkey": "X2KI57TA8"}}
    store.complete_xml(task, None, "xml/a.xml", "0" * 64, parsed,
                       {"mst": "0100112233", "ten": "NCC A", "dia_chi": "HN"})

    assert _count(store, "SELECT status FROM tasks WHERE task_type='xml'") == "done"
    assert _count(store, "SELECT name FROM partners WHERE mst='0100112233'") == "NCC A"
    [pdf] = store.claim_tasks("pdf", COMPANY, 10)
    assert pdf["mst_tcgp"] == "0105987432" and pdf["tt_khac"] == {"Fkey": "X2KI57TA8"}
    assert pdf["source_id"] is None

    store.mark_no_source(pdf, "chưa có adapter", None)
    assert store.claim_tasks("pdf", COMPANY, 10) == []
    assert store.set_provider_source("0105987432", "21_easyinvoice_tracuu") == 1
    [pdf] = store.claim_tasks("pdf", COMPANY, 10)
    assert pdf["source_id"] == "21_easyinvoice_tracuu"


def test_summary_only_invoice_skips_xml_download(store):
    raw = {"nbmst": "0100686209", "nbten": "MOBIFONE", "khhdon": "K26TAA", "shdon": 9,
           "khmshdon": 1, "tdlap": "2026-10-11T17:00:00Z", "tgtttbso": 55000, "hthdon": 2}
    assert store.upsert_invoices([job_policy.normalize_list_row(raw, COMPANY, "mua-khma")]) == 1
    assert store.claim_tasks("xml", COMPANY, 10) == []                   # không gọi Thuế
    assert _count(store, "SELECT error_kind FROM tasks") == "summary_only"
    assert _count(store, "SELECT hthdon FROM invoices") == 2             # phần đầu HĐ vẫn lưu
    rep = store.status_report()
    assert rep["summary_only"][0]["n"] == 1


def test_complete_xml_writes_invoice_details(store):
    store.upsert_invoices([_row(1)])
    [task] = store.claim_tasks("xml", COMPANY, 10)
    items = [{"stt": 1, "tinh_chat": 1, "ten_hhdv": "Dầu DO", "don_vi_tinh": "Lít",
              "so_luong": 73.032, "don_gia": 30520, "thanh_tien": 2228937, "thue_suat": "KKKNT",
              "tien_thue": 0, "tong_tien": 2228937, "tt_khac": {"Extra1": "1"}},
             {"tinh_chat": 4, "ten_hhdv": "Ghi chú"}]
    store.complete_xml(task, None, "xml/a.xml", "0" * 64,
                       {"thong_tin_chung": {}, "hang_hoa": items}, None)
    with store._tx() as cur:
        cur.execute("SELECT line_no, so_luong, thanh_tien, extra, nguon FROM invoice_details "
                    "ORDER BY line_no")
        rows = cur.fetchall()
    assert [r["line_no"] for r in rows] == [1, 2]
    assert str(rows[0]["so_luong"]) == "73.032000" and rows[0]["extra"] == {"Extra1": "1"}
    assert rows[0]["nguon"] == "xml" and rows[1]["thanh_tien"] is None
    # ghi lại (vd XML về lại) → thay, không nhân đôi
    with store._tx() as cur:
        store.replace_details(cur, task["id"], items[:1], "xml_email")
    assert _count(store, "SELECT count(*) FROM invoice_details") == 1


def test_no_portal_is_not_auto_requeued(store):
    store.upsert_invoices([_row(1), _row(2)])
    for t in store.claim_tasks("xml", COMPANY, 10):
        store.complete_xml(t, None, "xml/a.xml", "0" * 64,
                           {"thong_tin_chung": {"mst_tcgp": "0100684378"}}, None)
    a, b = store.claim_tasks("pdf", COMPANY, 10)
    store.mark_no_source(a, "chưa có adapter", None)                     # kind no_source
    store.mark_no_source(b, "chưa biết cổng", None, kind="no_portal")
    # tick chỉ tự requeue loại 'no_source'
    assert store.requeue("no_source", task_type="pdf", mst_tcgp="0100684378",
                         error_kind="no_source") == 1
    # bổ sung cổng xong → requeue thủ công theo MSTTCGP lấy nốt
    assert store.requeue("no_source", task_type="pdf", mst_tcgp="0100684378") == 1


def test_token_cache(store):
    now = datetime.now(timezone.utc)
    store.save_token(COMPANY, "tok-1", now + timedelta(hours=1))
    assert store.get_token(COMPANY) == "tok-1"
    store.save_token(COMPANY, "tok-2", now + timedelta(minutes=5))      # sắp hết hạn
    assert store.get_token(COMPANY, min_ttl_seconds=600) is None
    store.save_token(COMPANY, "tok-3", now + timedelta(hours=1))
    store.invalidate_token(COMPANY)
    assert store.get_token(COMPANY) is None


def test_cursor_never_moves_back(store):
    store.set_cursor(COMPANY, "mua", date(2026, 10, 10))
    store.set_cursor(COMPANY, "mua", date(2026, 9, 1))
    assert store.get_cursor(COMPANY, "mua") == date(2026, 10, 10)


def test_lock_is_exclusive(store):
    other = InvoiceStore(DSN)
    other.lock_key = store.lock_key
    try:
        assert store.try_lock()
        assert not other.try_lock()
        store.unlock()
        assert other.try_lock()
        other.unlock()
    finally:
        other.close()


# ---------------------------------------------------------------------------
# Bước XML đầu-cuối với GDT giả
# ---------------------------------------------------------------------------

XML = (b'<HDon><DLHDon><TTChung><KHMSHDon>1</KHMSHDon><KHHDon>C26TAA</KHHDon>'
       b'<SHDon>1</SHDon><MSTTCGP>0105987432</MSTTCGP></TTChung><NDHDon>'
       b'<NBan><Ten>NCC A</Ten><MST>0100112233</MST></NBan><NMua><MST>1201496252</MST></NMua>'
       b'</NDHDon></DLHDon></HDon>')


def _zip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hd.xml", data)
    return buf.getvalue()


class _Tokens:
    mst = COMPANY

    def get(self):
        return "tok"

    def refresh(self):
        return "tok"


def test_process_xml_end_to_end(store, tmp_path, monkeypatch):
    job = _load_invoice_job()
    monkeypatch.setattr(job, "XML_THROTTLE_SECONDS", 0)
    responses = {1: _zip(XML), 2: api_client.GDTError("chưa có", "not_ready")}

    def fake_download(token, inv):
        r = responses[inv["shdon"]]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(job.api_client, "download_invoice_zip", fake_download)

    store.upsert_invoices([_row(1), _row(2)])
    company = store.active_companies()[0]
    stats = Counter()
    job.process_xml(store, company, _Tokens(), job_policy.CircuitBreaker(), None, 100, stats, tmp_path)

    assert stats["xml_ok"] == 1 and stats["xml_failed"] == 1
    f = tmp_path / "xml" / COMPANY / "2026" / "10" / "0100112233" / "1_C26TAA_1.xml"
    assert f.read_bytes() == XML
    assert _count(store, "SELECT count(*) FROM tasks WHERE task_type='pdf'") == 1
    nxt = _count(store, "SELECT next_run_at FROM tasks t JOIN invoices i ON i.id=t.invoice_id "
                        "WHERE i.shdon=2 AND t.task_type='xml'")
    assert nxt > datetime.now(timezone.utc) + timedelta(hours=23)       # not_ready → chờ 1 ngày

    # Đồng bộ lại danh sách: HĐ đã xử lý không bị lấy lại
    assert store.upsert_invoices([_row(1), _row(2)]) == 0
    stats2 = Counter()
    job.process_xml(store, company, _Tokens(), job_policy.CircuitBreaker(), None, 100, stats2, tmp_path)
    assert stats2 == Counter()


def test_process_pdf_without_adapter_parks_task(store, tmp_path):
    job = _load_invoice_job()
    store.upsert_invoices([_row(1)])
    [task] = store.claim_tasks("xml", COMPANY, 10)
    store.complete_xml(task, None, "xml/a.xml", "0" * 64,
                       {"thong_tin_chung": {"mst_tcgp": "0105987432"}}, None)
    stats = Counter()
    job.process_pdf(store, store.active_companies()[0], None, 50, stats, tmp_path)
    assert stats["pdf_no_source"] == 1
    assert _count(store, "SELECT status FROM tasks WHERE task_type='pdf'") == "no_source"
