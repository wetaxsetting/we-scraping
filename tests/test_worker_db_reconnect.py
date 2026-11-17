"""Worker chạy mãi phải sống qua lúc máy chủ DB khởi động lại.

16/11/2026 16:12 PostgreSQL (192.168.60.121) khởi động lại: worker XML, PDF và WeInvoice cùng gặp lỗi
kết nối, thoát mã 1, và Task Scheduler không bật lại (RestartCount chỉ áp dụng khi task không khởi
động được). Cả hệ thống ngừng tải XML/PDF gần 1 ngày mà không ai biết. Ở đây kiểm: worker nối lại DB
rồi chạy tiếp, trả lại task đang dở, đóng dòng job_runs đang mở, và lỗi không phải mất kết nối vẫn
nổi lên như cũ.

Run::

    python -X utf8 -m pytest tests/test_worker_db_reconnect.py -q
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "web")):
    if p not in sys.path:
        sys.path.insert(0, p)

import invoice_store  # noqa: E402
import weinvoice_worker as ww  # noqa: E402


def _load_invoice_job():
    spec = importlib.util.spec_from_file_location("invoice_job_reconnect", ROOT / "web" / "invoice_job.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


invoice_job = _load_invoice_job()
A = "1201496252"


def _lost():
    return psycopg2.OperationalError("server closed the connection unexpectedly")


# ---------------------------------------------------------------------------
# Nhận ra lỗi mất kết nối
# ---------------------------------------------------------------------------

class _QueryCanceled(psycopg2.OperationalError):
    pgcode = "57014"            # hủy câu lệnh: lỗi câu lệnh, không phải mất kết nối


class _AdminShutdown(psycopg2.OperationalError):
    pgcode = "57P01"


def test_connection_errors_are_recognised():
    assert invoice_store.is_connection_error(_lost())                       # không có SQLSTATE
    assert invoice_store.is_connection_error(psycopg2.InterfaceError("connection already closed"))
    assert invoice_store.is_connection_error(_AdminShutdown("terminating connection"))
    assert not invoice_store.is_connection_error(_QueryCanceled("canceling statement"))
    assert not invoice_store.is_connection_error(ValueError("x"))
    assert not invoice_store.is_connection_error(None)


def test_connection_error_wrapped_in_another_error_is_recognised():
    try:
        try:
            raise _lost()
        except psycopg2.Error as e:
            raise RuntimeError("ghi kết quả lỗi") from e
    except RuntimeError as wrapped:
        assert invoice_store.is_connection_error(wrapped)


# ---------------------------------------------------------------------------
# InvoiceStore.reconnect
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, log):
        self.log, self.autocommit, self.closed = log, True, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self, cursor_factory=None):
        log = self.log

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                log.append(sql)
        return _Cur()

    def close(self):
        self.closed = 1
        self.log.append("close")


def test_reconnect_opens_a_new_connection_and_reapplies_the_schema(monkeypatch):
    log, conns = [], []

    def connect(dsn):
        conns.append(_FakeConn(log))
        return conns[-1]

    monkeypatch.setattr(invoice_store.psycopg2, "connect", connect)
    store = invoice_store.InvoiceStore("postgresql://x", schema="tmp_test")
    first = store.conn
    store.reconnect()
    assert store.conn is conns[1] and store.conn is not first
    assert first.closed == 1 and store.conn.autocommit is False
    assert log.count("SET search_path TO tmp_test") == 2


# ---------------------------------------------------------------------------
# wait_for_db
# ---------------------------------------------------------------------------

def test_wait_for_db_retries_with_backoff_until_the_server_is_back():
    slept, calls = [], {"n": 0}

    class Store:
        def reconnect(self):
            calls["n"] += 1
            if calls["n"] < 4:
                raise psycopg2.OperationalError("could not connect to server")

    assert invoice_job.wait_for_db(Store(), first=5, max_wait=20, sleep=slept.append) == 4
    assert slept == [5, 10, 20, 20]


# ---------------------------------------------------------------------------
# Worker XML/PDF (invoice_job worker)
# ---------------------------------------------------------------------------

def _task(tid):
    return {"company_mst": A, "task_id": tid, "attempts": 0, "nbmst": "0312650437",
            "khmshdon": 1, "khhdon": "C26TAA", "shdon": tid}


class _Store:
    """Store giả: `fail_on_begin` lần đầu ghi job_run_companies thì ném lỗi `fail_with`."""

    def __init__(self, batches, fail_with=None):
        self.batches = list(batches)
        self.fail_with = fail_with
        self.released, self.runs, self.reconnects = [], [], 0

    def reclaim_stale(self, minutes=60):
        return 0

    def claim_round_robin(self, task_type, per_company, limit, exclude_msts=()):
        return self.batches.pop(0) if self.batches else []

    def active_companies(self):
        return [{"company_mst": A, "gdt_username": A}]

    def release_tasks(self, ids, delay_minutes=0):
        if ids:
            self.released.append(list(ids))

    def start_run(self, command):
        self.runs.append([command, None, None])
        return len(self.runs)

    def finish_run(self, run_id, status, stats):
        self.runs[run_id - 1][1:] = [status, stats]

    def begin_run_company(self, run_id, mst):
        if self.fail_with is not None:
            e, self.fail_with = self.fail_with, None
            raise e

    def finish_run_company(self, run_id, mst, status, stats):
        pass

    def reconnect(self):
        self.reconnects += 1


@pytest.fixture
def xml_seen(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(invoice_job.time, "sleep", lambda s: None)
    monkeypatch.setattr(invoice_job, "XML_THROTTLE_SECONDS", 0)
    monkeypatch.setattr(invoice_job, "output_dir", lambda: tmp_path)
    monkeypatch.setattr(invoice_job, "TokenProvider", lambda store, company, stats: object())
    monkeypatch.setattr(invoice_job, "_do_xml_task",
                        lambda store, company, tokens, task, run_id, out_dir: seen.append((task["task_id"], run_id)))
    return seen


def _args():
    return argparse.Namespace(type="xml", per_company=20, batch=200, idle=0, once=True)


def test_worker_survives_a_db_restart_mid_batch(xml_seen):
    store = _Store([[_task(1), _task(2)], [_task(1), _task(2)]], fail_with=_lost())
    waits = []

    def wait(s):
        waits.append(s)
        s.reconnect()

    assert invoice_job.cmd_worker(store, _args(), wait=wait) == 0      # không thoát mã lỗi
    assert waits == [store] and store.reconnects == 1
    assert store.released == [[1, 2]]                  # task mẻ dở được trả lại sau khi nối lại
    assert store.runs[0] == ["worker-xml", "error", {"db_connection_lost": 1}]   # lượt dở được đóng
    assert xml_seen == [(1, 2), (2, 2)]                # mẻ sau làm lại đủ, trong lượt mới
    assert store.runs[1][1] == "ok"


def test_worker_survives_a_db_error_before_any_batch(xml_seen):
    class Store(_Store):
        def reclaim_stale(self, minutes=60):
            if not self.reconnects:
                raise psycopg2.InterfaceError("connection already closed")
            return 0

    store = Store([[_task(5)]])
    assert invoice_job.cmd_worker(store, _args(), wait=lambda s: s.reconnect()) == 0
    assert store.reconnects == 1 and xml_seen == [(5, 1)]


def test_other_errors_still_stop_the_worker_and_release_tasks(xml_seen):
    store = _Store([[_task(1), _task(2)]], fail_with=ValueError("lỗi code"))
    with pytest.raises(ValueError):
        invoice_job.cmd_worker(store, _args(), wait=lambda s: pytest.fail("không được chờ DB"))
    assert store.released == [[1, 2]]
    assert store.runs[0][1] == "error"


# ---------------------------------------------------------------------------
# Worker WeInvoice
# ---------------------------------------------------------------------------

class _Requests:
    s = "weinvoice"

    def __init__(self, rows):
        self.rows = list(rows)
        self.finished = []

    def available(self):
        return True

    def fail_stale(self, minutes=None):
        return 0

    def claim(self, types):
        return self.rows.pop(0) if self.rows else None

    def report(self, *a):
        return False

    def finish(self, rqst_seq, status, result=None, error=None, job_run_id=None):
        self.finished.append((rqst_seq, status, error))


def test_weinvoice_worker_survives_a_db_restart_and_fails_the_interrupted_request():
    row = {"owner_id": "u@x", "cmpn_mst": A, "rqst_type": "SYNC", "rqst_prmt": {}}
    calls = []

    def handler(job):
        calls.append(job.id)
        if job.id == 30:
            raise _lost()
        return "DONE", {"new": 0}

    w = object.__new__(ww.Worker)
    w.store = type("S", (), {"reconnects": 0, "reconnect": lambda self: setattr(self, "reconnects", self.reconnects + 1)})()
    w.reqs = _Requests([{**row, "rqst_seq": 30}, {**row, "rqst_seq": 31}])
    w.handlers = {"SYNC": handler}
    w._interrupted = []

    assert w.run(once=True, wait=lambda s: s.reconnect()) == 0
    assert w.store.reconnects == 1
    assert calls == [30, 31]                                           # yêu cầu sau vẫn được làm
    assert w.reqs.finished == [(30, "FAILED", ww.DB_LOST_MESSAGE), (31, "DONE", None)]


def test_weinvoice_worker_other_errors_do_not_trigger_reconnect():
    row = {"rqst_seq": 9, "owner_id": "u@x", "cmpn_mst": A, "rqst_type": "SYNC", "rqst_prmt": {}}
    w = object.__new__(ww.Worker)
    w.store = object()
    w.reqs = _Requests([row])
    w.handlers = {"SYNC": lambda job: (_ for _ in ()).throw(KeyError("x"))}
    w._interrupted = []
    assert w.run(once=True, wait=lambda s: pytest.fail("không được chờ DB")) == 0
    assert w.reqs.finished[0][1] == "FAILED" and w.reqs.finished[0][2].startswith("Lỗi engine: KeyError")
